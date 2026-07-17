from __future__ import annotations

from contextlib import asynccontextmanager
from threading import Event

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from api import accounts, ai, assets, billing, jobs, system, tickets
from api.support import require_identity, resolve_web_asset, start_limited_account_watcher
from services.account_service import account_service
from services.audit_service import audit_service
from services.chatgpt_service import ChatGPTService
from services.config import config
from services.image_job_service import image_job_service, start_image_job_worker


def create_app() -> FastAPI:
    chatgpt_service = ChatGPTService(account_service)
    app_version = config.app_version

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        stop_event = Event()
        account_thread = start_limited_account_watcher(stop_event)
        image_job_thread = start_image_job_worker(stop_event, image_job_service, chatgpt_service, base_url=config.base_url)
        config.cleanup_old_images()
        try:
            yield
        finally:
            stop_event.set()
            account_thread.join(timeout=1)
            image_job_thread.join(timeout=1)

    app = FastAPI(title="chatgpt2api", version=app_version, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.web_allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def https_redirect_response(request: Request) -> RedirectResponse | None:
        if not config.force_https:
            return None
        forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
        scheme = forwarded_proto or request.url.scheme
        if scheme == "https" or request.url.path.startswith("/health/"):
            return None
        target = request.url.replace(scheme="https")
        return RedirectResponse(str(target), status_code=308)

    def add_security_headers(response) -> None:
        if not config.security_headers_enabled:
            return
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault("Content-Security-Policy", config.content_security_policy)
        if config.hsts_enabled:
            response.headers.setdefault(
                "Strict-Transport-Security",
                f"max-age={config.hsts_max_age_seconds}; includeSubDomains",
            )

    @app.middleware("http")
    async def security_headers_and_https(request: Request, call_next):
        redirect = https_redirect_response(request)
        if redirect is not None:
            add_security_headers(redirect)
            return redirect
        response = await call_next(request)
        add_security_headers(response)
        return response

    @app.middleware("http")
    async def cookie_session_to_authorization(request: Request, call_next):
        if not request.headers.get("authorization"):
            session_cookie_name = "chatgpt2api_session"
            try:
                from api import system as system_api
                session_cookie_name = system_api.AUTH_SESSION_COOKIE_NAME
            except Exception:
                pass
            session_token = str(request.cookies.get(session_cookie_name) or "").strip()
            if session_token:
                headers = list(request.scope.get("headers") or [])
                headers.append((b"authorization", f"Bearer {session_token}".encode("latin-1")))
                request.scope["headers"] = headers
        return await call_next(request)

    def should_audit_admin_request(request: Request) -> bool:
        if request.method.upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
            return False
        path = request.url.path
        admin_prefixes = (
            "/api/admin/",
            "/api/accounts",
            "/api/auth/users",
            "/api/cpa/",
            "/api/sub2api/",
            "/api/settings",
            "/api/proxy",
        )
        return any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in admin_prefixes)

    def record_admin_request_audit(
            request: Request,
            identity: dict[str, object] | None,
            *,
            status_code: int,
            failed: bool,
    ) -> None:
        try:
            audit_service.record(
                "admin.http_request",
                actor=identity,
                target_type="http",
                target_id=f"{request.method.upper()} {request.url.path}",
                status="failed" if failed or status_code >= 400 else "succeeded",
                summary="admin mutation request failed" if failed else "admin mutation request",
                detail={"method": request.method.upper(), "path": request.url.path, "status_code": status_code},
                request=request,
            )
        except Exception as exc:
            print(f"[audit] failed to write admin request audit log: {exc}")

    @app.middleware("http")
    async def audit_admin_mutations(request: Request, call_next):
        should_audit = should_audit_admin_request(request)
        identity = None
        if should_audit:
            try:
                identity = require_identity(request.headers.get("authorization"))
            except HTTPException:
                identity = None
        try:
            response = await call_next(request)
        except Exception:
            if should_audit:
                record_admin_request_audit(request, identity, status_code=500, failed=True)
            raise
        if should_audit:
            record_admin_request_audit(request, identity, status_code=response.status_code, failed=False)
        return response

    app.include_router(ai.create_router(chatgpt_service))
    app.include_router(jobs.create_router(chatgpt_service))
    app.include_router(assets.create_router())
    app.include_router(billing.create_router())
    app.include_router(tickets.create_router())
    app.include_router(accounts.create_router())
    app.include_router(system.create_router(app_version))
    if config.assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(config.assets_dir)), name="assets")
    if config.images_dir.exists():
        app.mount("/images", StaticFiles(directory=str(config.images_dir)), name="images")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_web(full_path: str):
        asset = resolve_web_asset(full_path)
        if asset is not None:
            return FileResponse(asset)
        if full_path.strip("/").startswith("_next/"):
            raise HTTPException(status_code=404, detail="Not Found")
        fallback = resolve_web_asset("")
        if fallback is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(fallback)

    return app
