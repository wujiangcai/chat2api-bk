from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from api.support import (
    check_quota,
    check_rate_limit,
    require_admin,
    require_identity,
    require_permission,
    resolve_image_base_url,
)
from services.auth_service import auth_service
from services.image_job_service import ImageJobService, image_job_service


class ImageJobGenerationRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    model: str = "gpt-image-2"
    n: int = Field(default=1, ge=1, le=4)
    size: str | None = None
    response_format: str = "b64_json"


class ImageJobRetryRequest(BaseModel):
    reason: str = "admin-retry"


def _reserve_job_quota(identity: dict[str, object], job_id: str, units: int) -> int:
    if identity.get("role") == "admin":
        return 0
    requested = max(1, int(units or 1))
    user_id = str(identity.get("user_id") or "").strip()
    if user_id:
        ok = auth_service.try_consume_user_quota(
            user_id,
            requested,
            reason="image-job-reserve",
            ref_type="image_job",
            ref_id=job_id,
            metadata={"endpoint": "/api/jobs/images/generations"},
        )
        if not ok:
            raise HTTPException(status_code=429, detail={"error": "quota exceeded", "quota_balance": identity.get("quota_balance")})
        return requested
    check_quota(identity, requested)
    return 0


def _refund_job_quota(identity: dict[str, object], job_id: str, units: int) -> None:
    user_id = str(identity.get("user_id") or "").strip()
    if user_id and units > 0:
        auth_service.refund_user_quota(
            user_id,
            units,
            reason="refund",
            ref_type="image_job",
            ref_id=job_id,
            metadata={"refund_reason": "enqueue-failed"},
        )


def create_router(chatgpt_service, job_service: ImageJobService | None = None) -> APIRouter:
    router = APIRouter()
    jobs = job_service or image_job_service

    @router.post("/api/jobs/images/generations", status_code=202)
    async def enqueue_image_generation(
            body: ImageJobGenerationRequest,
            request: Request,
            authorization: str | None = Header(default=None),
    ):
        identity = require_permission(authorization, "image.generate")
        check_rate_limit(identity, body.n)
        job_id = jobs.new_job_id()
        reserved_quota = _reserve_job_quota(identity, job_id, body.n)
        try:
            job = jobs.enqueue_generation(
                job_id=job_id,
                identity=identity,
                request=body.model_dump(mode="python"),
                base_url=resolve_image_base_url(request),
                reserved_quota=reserved_quota,
            )
        except Exception:
            _refund_job_quota(identity, job_id, reserved_quota)
            raise
        return {"job": job}

    @router.get("/api/jobs")
    async def list_my_jobs(
            status: str = "",
            limit: int = Query(default=50, ge=1, le=500),
            authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        return {"items": jobs.list_jobs(identity, limit=limit, status=status or None)}

    @router.get("/api/jobs/{job_id}")
    async def get_my_job(job_id: str, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        job = jobs.get_job_for_identity(job_id, identity)
        if job is None:
            raise HTTPException(status_code=404, detail={"error": "job not found"})
        return {"job": job}

    @router.post("/api/jobs/{job_id}/cancel")
    async def cancel_my_job(job_id: str, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        try:
            job = jobs.cancel_job(job_id, identity)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        if job is None:
            raise HTTPException(status_code=404, detail={"error": "job not found"})
        return {"job": job}

    @router.get("/api/admin/jobs")
    async def list_admin_jobs(
            status: str = "",
            limit: int = Query(default=100, ge=1, le=500),
            authorization: str | None = Header(default=None),
    ):
        identity = require_admin(authorization)
        return {"items": jobs.list_jobs(identity, limit=limit, status=status or None)}

    @router.get("/api/admin/jobs/dead-letter")
    async def list_admin_dead_letter_jobs(
            limit: int = Query(default=100, ge=1, le=500),
            authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        return {"items": jobs.list_dead_letter_jobs(limit=limit)}

    @router.post("/api/admin/jobs/run-next")
    async def run_next_admin_job(request: Request, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        job = await run_in_threadpool(jobs.run_next, chatgpt_service, resolve_image_base_url(request))
        return {"job": job}

    @router.post("/api/admin/jobs/{job_id}/retry")
    async def retry_admin_dead_letter_job(
            job_id: str,
            body: ImageJobRetryRequest | None = None,
            authorization: str | None = Header(default=None),
    ):
        actor = require_admin(authorization)
        try:
            job = await run_in_threadpool(jobs.retry_dead_letter_job, job_id, actor=actor, reason=(body.reason if body else "admin-retry"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        if job is None:
            raise HTTPException(status_code=404, detail={"error": "job not found"})
        return {"job": job}

    @router.post("/api/admin/jobs/recover-stale")
    async def recover_stale_admin_jobs(
            stale_after_seconds: int | None = Query(default=None, ge=1),
            authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        recovered = await run_in_threadpool(jobs.recover_stale_running_jobs, stale_after_seconds=stale_after_seconds)
        return {"items": recovered, "recovered": len(recovered)}

    return router
