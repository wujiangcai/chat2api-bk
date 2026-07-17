from __future__ import annotations

from datetime import datetime
import time

from fastapi import APIRouter, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from api.support import (
    check_quota,
    check_rate_limit,
    consume_quota,
    count_success_items,
    raise_image_quota_error,
    refund_quota,
    require_identity,
    require_permission,
    reserve_quota,
    resolve_image_base_url,
)
from services.account_service import account_service
from services.chatgpt_service import ChatGPTService, ImageGenerationError
from services.image_asset_service import image_asset_service
from services.log_service import (
    LOG_TYPE_CALL,
    log_service,
)
from utils.helper import anthropic_sse_stream, is_image_chat_request, sse_json_stream


class ImageGenerationRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    model: str = "gpt-image-2"
    n: int = Field(default=1, ge=1, le=4)
    size: str | None = None
    response_format: str = "b64_json"
    history_disabled: bool = True
    stream: bool | None = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str | None = None
    prompt: str | None = None
    n: int | None = None
    stream: bool | None = None
    modalities: list[str] | None = None
    messages: list[dict[str, object]] | None = None


class ResponseCreateRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str | None = None
    input: object | None = None
    tools: list[dict[str, object]] | None = None
    tool_choice: object | None = None
    stream: bool | None = None


class AnthropicMessageRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str | None = None
    messages: list[dict[str, object]] | None = None
    system: object | None = None
    stream: bool | None = None


def _identity_detail(identity: dict[str, object]) -> dict[str, object]:
    return {"key_id": identity.get("id"), "key_name": identity.get("name"), "role": identity.get("role")}


def _collect_urls(value: object) -> list[str]:
    urls: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "url" and isinstance(item, str):
                urls.append(item)
            elif key == "urls" and isinstance(item, list):
                urls.extend(str(url) for url in item if isinstance(url, str))
            else:
                urls.extend(_collect_urls(item))
    elif isinstance(value, list):
        for item in value:
            urls.extend(_collect_urls(item))
    return urls


def _log_call(summary: str, identity: dict[str, object], endpoint: str, model: str, started: float, result: object = None, status: str = "success") -> None:
    detail = {
        **_identity_detail(identity),
        "endpoint": endpoint,
        "model": model,
        "started_at": datetime.fromtimestamp(started).strftime("%Y-%m-%d %H:%M:%S"),
        "ended_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "duration_ms": int((time.time() - started) * 1000),
        "status": status,
    }
    urls = _collect_urls(result)
    if urls:
        detail["urls"] = urls
    log_service.add(LOG_TYPE_CALL, summary, detail)


def _archive_image_assets(
        identity: dict[str, object],
        result: object,
        *,
        endpoint: str,
        model: str,
        prompt: str,
        base_url: str,
) -> None:
    try:
        image_asset_service.archive_result(
            owner=identity,
            result=result,
            source=endpoint,
            model=model,
            prompt=prompt,
            base_url=base_url,
        )
    except Exception as exc:
        _log_call("图片资产归档失败", identity, endpoint, model, time.time(), {"error": str(exc)}, "failed")


def _stream_with_log(
        items,
        summary: str,
        identity: dict[str, object],
        endpoint: str,
        model: str,
        started: float,
        quota_fallback: int = 0,
        reserved_quota: int = 0,
):
    urls: list[str] = []
    failed = False
    try:
        for item in items:
            urls.extend(_collect_urls(item))
            yield item
    except Exception:
        failed = True
        refund_quota(identity, reserved_quota)
        _log_call(summary.replace("结束", "失败"), identity, endpoint, model, started, {"urls": list(dict.fromkeys(urls))}, "failed")
        raise
    finally:
        if not failed:
            unique_urls = list(dict.fromkeys(urls))
            _log_call(summary, identity, endpoint, model, started, {"urls": unique_urls})
            if reserved_quota <= 0 and quota_fallback > 0:
                consume_quota(identity, len(unique_urls) or quota_fallback)


def create_router(chatgpt_service: ChatGPTService) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/models")
    async def list_models(authorization: str | None = Header(default=None)):
        require_identity(authorization)
        try:
            return await run_in_threadpool(chatgpt_service.list_models)
        except Exception as exc:
            raise HTTPException(status_code=502, detail={"error": str(exc)}) from exc

    @router.post("/v1/images/generations")
    async def generate_images(
            body: ImageGenerationRequest,
            request: Request,
            authorization: str | None = Header(default=None),
    ):
        identity = require_permission(authorization, "image.generate")
        check_rate_limit(identity, body.n)
        reserved_quota = reserve_quota(identity, body.n)
        started = time.time()
        base_url = resolve_image_base_url(request)
        if body.stream:
            try:
                await run_in_threadpool(account_service.get_available_access_token)
            except RuntimeError as exc:
                refund_quota(identity, reserved_quota)
                raise_image_quota_error(exc)
            return StreamingResponse(
                sse_json_stream(
                    _stream_with_log(
                        chatgpt_service.stream_image_generation(
                            body.prompt, body.model, body.n, body.size, body.response_format, base_url
                        ),
                        "文生图流式调用结束",
                        identity,
                        "/v1/images/generations",
                        body.model,
                        started,
                        body.n,
                        reserved_quota,
                    )
                ),
                media_type="text/event-stream",
            )
        try:
            result = await run_in_threadpool(
                chatgpt_service.generate_with_pool, body.prompt, body.model, body.n, body.size, body.response_format, base_url
            )
            await run_in_threadpool(_archive_image_assets, identity, result, endpoint="/v1/images/generations", model=body.model, prompt=body.prompt, base_url=base_url)
            _log_call("文生图调用完成", identity, "/v1/images/generations", body.model, started, result)
            if reserved_quota <= 0:
                consume_quota(identity, count_success_items(result, body.n))
            return result
        except ImageGenerationError as exc:
            refund_quota(identity, reserved_quota)
            _log_call("文生图调用失败", identity, "/v1/images/generations", body.model, started, {"error": str(exc)}, "failed")
            raise_image_quota_error(exc)
        except Exception as exc:
            refund_quota(identity, reserved_quota)
            _log_call("文生图调用失败", identity, "/v1/images/generations", body.model, started, {"error": str(exc)}, "failed")
            raise

    @router.post("/v1/images/edits")
    async def edit_images(
            request: Request,
            authorization: str | None = Header(default=None),
            image: list[UploadFile] | None = File(default=None),
            image_list: list[UploadFile] | None = File(default=None, alias="image[]"),
            prompt: str = Form(...),
            model: str = Form(default="gpt-image-2"),
            n: int = Form(default=1),
            size: str | None = Form(default=None),
            response_format: str = Form(default="b64_json"),
            stream: bool | None = Form(default=None),
    ):
        identity = require_permission(authorization, "image.edit")
        if n < 1 or n > 4:
            raise HTTPException(status_code=400, detail={"error": "n must be between 1 and 4"})
        check_rate_limit(identity, n)
        started = time.time()
        uploads = [*(image or []), *(image_list or [])]
        if not uploads:
            raise HTTPException(status_code=400, detail={"error": "image file is required"})
        base_url = resolve_image_base_url(request)
        images: list[tuple[bytes, str, str]] = []
        for upload in uploads:
            image_data = await upload.read()
            if not image_data:
                raise HTTPException(status_code=400, detail={"error": "image file is empty"})
            images.append((image_data, upload.filename or "image.png", upload.content_type or "image/png"))
        reserved_quota = reserve_quota(identity, n)
        if stream:
            if not account_service.has_available_account():
                refund_quota(identity, reserved_quota)
                raise_image_quota_error(RuntimeError("no available image quota"))
            return StreamingResponse(
                sse_json_stream(_stream_with_log(
                    chatgpt_service.stream_image_edit(prompt, images, model, n, size, response_format, base_url),
                    "图生图流式调用结束",
                    identity,
                    "/v1/images/edits",
                    model,
                    started,
                    n,
                    reserved_quota,
                )),
                media_type="text/event-stream",
            )
        try:
            result = await run_in_threadpool(
                chatgpt_service.edit_with_pool, prompt, images, model, n, size, response_format, base_url
            )
            await run_in_threadpool(_archive_image_assets, identity, result, endpoint="/v1/images/edits", model=model, prompt=prompt, base_url=base_url)
            _log_call("图生图调用完成", identity, "/v1/images/edits", model, started, result)
            if reserved_quota <= 0:
                consume_quota(identity, count_success_items(result, n))
            return result
        except ImageGenerationError as exc:
            refund_quota(identity, reserved_quota)
            _log_call("图生图调用失败", identity, "/v1/images/edits", model, started, {"error": str(exc)}, "failed")
            raise_image_quota_error(exc)
        except Exception as exc:
            refund_quota(identity, reserved_quota)
            _log_call("图生图调用失败", identity, "/v1/images/edits", model, started, {"error": str(exc)}, "failed")
            raise

    @router.post("/v1/chat/completions")
    async def create_chat_completion(body: ChatCompletionRequest, authorization: str | None = Header(default=None)):
        identity = require_permission(authorization, "chat.completions")
        check_rate_limit(identity, 1)
        started = time.time()
        payload = body.model_dump(mode="python")
        model = str(payload.get("model") or "auto")
        if bool(payload.get("stream")):
            if is_image_chat_request(payload):
                try:
                    await run_in_threadpool(account_service.get_available_access_token)
                except RuntimeError as exc:
                    raise_image_quota_error(exc)
            return StreamingResponse(
                sse_json_stream(_stream_with_log(chatgpt_service.stream_chat_completion(payload), "文本生成流式调用结束", identity, "/v1/chat/completions", model, started)),
                media_type="text/event-stream",
            )
        try:
            result = await run_in_threadpool(chatgpt_service.create_chat_completion, payload)
            _log_call("文本生成调用完成", identity, "/v1/chat/completions", model, started, result)
            return result
        except Exception as exc:
            _log_call("文本生成调用失败", identity, "/v1/chat/completions", model, started, {"error": str(exc)}, "failed")
            raise

    @router.post("/v1/responses")
    async def create_response(body: ResponseCreateRequest, authorization: str | None = Header(default=None)):
        identity = require_permission(authorization, "responses.create")
        check_rate_limit(identity, 1)
        started = time.time()
        payload = body.model_dump(mode="python")
        model = str(payload.get("model") or "auto")
        if bool(payload.get("stream")):
            return StreamingResponse(
                sse_json_stream(_stream_with_log(chatgpt_service.stream_response(payload), "Responses 流式调用结束", identity, "/v1/responses", model, started)),
                media_type="text/event-stream",
            )
        try:
            result = await run_in_threadpool(chatgpt_service.create_response, payload)
            _log_call("Responses 调用完成", identity, "/v1/responses", model, started, result)
            return result
        except Exception as exc:
            _log_call("Responses 调用失败", identity, "/v1/responses", model, started, {"error": str(exc)}, "failed")
            raise

    @router.post("/v1/messages")
    async def create_message(
            body: AnthropicMessageRequest,
            authorization: str | None = Header(default=None),
            x_api_key: str | None = Header(default=None, alias="x-api-key"),
            anthropic_version: str | None = Header(default=None, alias="anthropic-version"),
    ):
        identity = require_permission(authorization or (f"Bearer {x_api_key}" if x_api_key else None), "messages.create")
        check_rate_limit(identity, 1)
        started = time.time()
        payload = body.model_dump(mode="python")
        model = str(payload.get("model") or "auto")
        if bool(payload.get("stream")):
            return StreamingResponse(
                anthropic_sse_stream(_stream_with_log(chatgpt_service.stream_message(payload), "Messages 流式调用结束", identity, "/v1/messages", model, started)),
                media_type="text/event-stream",
            )
        try:
            result = await run_in_threadpool(chatgpt_service.create_message, payload)
            _log_call("Messages 调用完成", identity, "/v1/messages", model, started, result)
            return result
        except Exception as exc:
            _log_call("Messages 调用失败", identity, "/v1/messages", model, started, {"error": str(exc)}, "failed")
            raise

    return router
