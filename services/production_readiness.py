from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from services.config import config
from services.email_service import EmailService
from services.payment_checkout_service import PaymentCheckoutService
from services.storage.base import StorageBackend
from services.storage.migrations.versions import ALL_MIGRATIONS


def _clean(value: object) -> str:
    return str(value or "").strip()


def _env_bool(value: object, default: bool = False) -> bool:
    raw = _clean(value)
    if not raw:
        return default
    return raw.lower() not in {"0", "false", "no", "off"}


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _is_placeholder(value: object) -> bool:
    raw = _clean(value).lower()
    if not raw:
        return True
    return any(marker in raw for marker in ("change-me", "your_", "example", "placeholder", "secret_key_here"))


def _origin_is_public_https(origin: str) -> bool:
    raw = origin.strip().rstrip("/")
    if not raw:
        return False
    parsed = urlsplit(raw)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https":
        return False
    if host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".localhost"):
        return False
    return True


class ProductionReadinessService:
    """Opinionated production launch checks for the commercial image service."""

    def __init__(
        self,
        *,
        config_obj: Any,
        storage_factory: Callable[[], StorageBackend],
        object_storage_factory: Callable[[], Any],
        image_job_service_factory: Callable[[], Any],
        env: Mapping[str, str] | None = None,
    ):
        self.config = config_obj
        self.storage_factory = storage_factory
        self.object_storage_factory = object_storage_factory
        self.image_job_service_factory = image_job_service_factory
        self.env = env if env is not None else os.environ

    def check(self, *, strict: bool = True) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        items.extend(self._check_app_config(strict=strict))
        items.extend(self._check_email_delivery())
        items.extend(self._check_storage())
        items.extend(self._check_queue())
        items.extend(self._check_object_storage())
        items.extend(self._check_backup_and_alerting())
        failed = [item for item in items if item["status"] == "failed"]
        warnings = [item for item in items if item["status"] == "warning"]
        status = "failed" if failed else "warning" if warnings else "passed"
        return {
            "status": status,
            "ready": not failed,
            "strict": strict,
            "summary": {
                "total": len(items),
                "passed": sum(1 for item in items if item["status"] == "passed"),
                "warning": len(warnings),
                "failed": len(failed),
            },
            "items": items,
        }

    @staticmethod
    def _item(check_id: str, status: str, message: str, *, detail: dict[str, Any] | None = None) -> dict[str, Any]:
        item: dict[str, Any] = {"id": check_id, "status": status, "message": message}
        if detail:
            item["detail"] = detail
        return item

    def _env(self, name: str) -> str:
        return _clean(self.env.get(name, ""))

    def _check_app_config(self, *, strict: bool) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []

        app_env_ok = bool(getattr(self.config, "is_production", False))
        items.append(
            self._item(
                "app.env.production",
                "passed" if app_env_ok else ("failed" if strict else "warning"),
                "APP_ENV is production" if app_env_ok else "APP_ENV/ENVIRONMENT/NODE_ENV is not production",
            )
        )

        base_url = _clean(getattr(self.config, "base_url", ""))
        base_url_ok = base_url.startswith("https://")
        items.append(
            self._item(
                "app.base_url.https",
                "passed" if base_url_ok else "failed",
                "CHATGPT2API_BASE_URL uses HTTPS" if base_url_ok else "CHATGPT2API_BASE_URL must be a public HTTPS URL",
                detail={"configured": bool(base_url)},
            )
        )

        origins = list(getattr(self.config, "web_allowed_origins", []) or [])
        origins_ok = bool(origins) and all(_origin_is_public_https(str(origin)) for origin in origins)
        items.append(
            self._item(
                "app.cors.public_https_origins",
                "passed" if origins_ok else "failed",
                "WEB_ALLOWED_ORIGINS contains only public HTTPS origins"
                if origins_ok
                else "WEB_ALLOWED_ORIGINS must be non-empty and must not include localhost/http origins in production",
                detail={"origin_count": len(origins)},
            )
        )

        security_ok = bool(getattr(self.config, "security_headers_enabled", False))
        hsts_ok = bool(getattr(self.config, "hsts_enabled", False))
        items.append(
            self._item(
                "app.security_headers",
                "passed" if security_ok and hsts_ok else "failed",
                "Security headers and HSTS are enabled"
                if security_ok and hsts_ok
                else "SECURITY_HEADERS_ENABLED and ENABLE_HSTS should both be enabled for production",
                detail={"security_headers_enabled": security_ok, "hsts_enabled": hsts_ok},
            )
        )

        auth_key_ok = not _is_placeholder(self._env("CHATGPT2API_AUTH_KEY") or getattr(self.config, "auth_key", ""))
        items.append(
            self._item(
                "app.auth_key.configured",
                "passed" if auth_key_ok else "failed",
                "CHATGPT2API_AUTH_KEY is configured with a non-placeholder value"
                if auth_key_ok
                else "CHATGPT2API_AUTH_KEY is missing or still uses a placeholder value",
            )
        )

        webhook_secrets = {
            key: value
            for key, value in self.env.items()
            if key == "PAYMENT_WEBHOOK_SECRET" or key.startswith("PAYMENT_WEBHOOK_SECRET_")
        }
        webhook_secret_ok = any(not _is_placeholder(value) for value in webhook_secrets.values())
        items.append(
            self._item(
                "payment.webhook_secret.configured",
                "passed" if webhook_secret_ok else "warning",
                "Payment webhook HMAC secret is configured"
                if webhook_secret_ok
                else "PAYMENT_WEBHOOK_SECRET or PAYMENT_WEBHOOK_SECRET_{PROVIDER} should be configured before enabling automatic payment callbacks",
                detail={"configured_secret_count": len(webhook_secrets)},
            )
        )

        checkout = PaymentCheckoutService(billing=None, env=self.env, config_obj=self.config)
        checkout_status = checkout.status()
        checkout_provider = _clean(checkout_status.get("provider")).lower()
        checkout_ok = bool(checkout_status.get("configured")) and checkout_provider not in {"disabled", "manual"}
        items.append(
            self._item(
                "payment.checkout.configured",
                "passed" if checkout_ok else "warning",
                "Customer payment checkout is configured"
                if checkout_ok
                else "Configure PAYMENT_CHECKOUT_PROVIDER=stripe or redirect before relying on self-service online payments",
                detail={
                    "provider": checkout_provider,
                    "configured": bool(checkout_status.get("configured")),
                    "message": checkout_status.get("message"),
                },
            )
        )

        business_name = self._env("BUSINESS_LEGAL_NAME")
        support_email = self._env("BUSINESS_SUPPORT_EMAIL") or self._env("SUPPORT_EMAIL")
        business_ok = bool(business_name) and bool(support_email)
        items.append(
            self._item(
                "business.legal_identity",
                "passed" if business_ok else "warning",
                "Business legal name and support email are configured for receipts"
                if business_ok
                else "Configure BUSINESS_LEGAL_NAME and BUSINESS_SUPPORT_EMAIL before issuing customer receipts",
                detail={"business_name_configured": bool(business_name), "support_email_configured": bool(support_email)},
            )
        )

        cookie_enabled = _env_bool(self._env("AUTH_SESSION_COOKIE_ENABLED"), True)
        response_includes_token = _env_bool(self._env("AUTH_RESPONSE_INCLUDE_TOKEN"), not getattr(self.config, "is_production", False))
        cookie_ok = cookie_enabled and not response_includes_token
        items.append(
            self._item(
                "auth.cookie_session",
                "passed" if cookie_ok else "warning",
                "HttpOnly cookie sessions are enabled and token responses are disabled"
                if cookie_ok
                else "Enable AUTH_SESSION_COOKIE_ENABLED and set AUTH_RESPONSE_INCLUDE_TOKEN=false before public launch",
                detail={"cookie_enabled": cookie_enabled, "response_includes_token": response_includes_token},
            )
        )

        email_verification_required = _env_bool(self._env("EMAIL_VERIFICATION_REQUIRED"), False)
        items.append(
            self._item(
                "auth.email_verification_required",
                "passed" if email_verification_required else "warning",
                "Email verification is required for password login"
                if email_verification_required
                else "EMAIL_VERIFICATION_REQUIRED should be enabled before opening public registration",
            )
        )

        rate_limit_backend = _clean(self._env("RATE_LIMIT_BACKEND") or "memory").lower()
        rate_limit_redis_url = self._env("RATE_LIMIT_REDIS_URL") or self._env("REDIS_URL")
        rate_limit_ok = rate_limit_backend in {"redis", "auto"} and bool(rate_limit_redis_url) and not _is_placeholder(rate_limit_redis_url)
        items.append(
            self._item(
                "rate_limit.redis",
                "passed" if rate_limit_ok else "failed",
                "Public/API rate limiting uses Redis"
                if rate_limit_ok
                else "Set RATE_LIMIT_BACKEND=redis and REDIS_URL/RATE_LIMIT_REDIS_URL for multi-replica public launch",
                detail={"backend": rate_limit_backend, "redis_url_configured": bool(rate_limit_redis_url)},
            )
        )
        return items

    def _check_email_delivery(self) -> list[dict[str, Any]]:
        email = EmailService(env=self.env, config_obj=self.config)
        status = email.status()
        provider = _clean(status.get("provider")).lower()
        public_base_url = _clean(email.public_base_url)
        public_url_ok = _origin_is_public_https(public_base_url)
        email_verification_required = _env_bool(self._env("EMAIL_VERIFICATION_REQUIRED"), False)
        delivery_ok = bool(status.get("configured")) and provider not in {"console", "disabled"} and public_url_ok
        if delivery_ok:
            check_status = "passed"
            message = "Email delivery provider and public action URL are configured"
        elif email_verification_required:
            check_status = "failed"
            message = "EMAIL_VERIFICATION_REQUIRED=true requires a real email provider and a public HTTPS APP_PUBLIC_URL"
        else:
            check_status = "warning"
            message = "Configure EMAIL_PROVIDER plus APP_PUBLIC_URL before enabling public email verification or password reset"
        return [
            self._item(
                "auth.email_delivery.configured",
                check_status,
                message,
                detail={
                    "provider": provider,
                    "provider_configured": bool(status.get("configured")),
                    "public_base_url_configured": bool(public_base_url),
                    "public_base_url_https": public_url_ok,
                },
            )
        ]

    def _check_storage(self) -> list[dict[str, Any]]:
        try:
            storage = self.storage_factory()
            info = dict(storage.get_backend_info())
            health = dict(storage.health_check())
        except Exception as exc:
            return [self._item("storage.postgres", "failed", "Storage backend check failed", detail={"error": str(exc)})]

        db_type = _clean(info.get("db_type")).lower()
        storage_ok = info.get("type") == "database" and db_type == "postgresql" and health.get("status") == "healthy"
        migration_count = _safe_int(health.get("schema_migration_count"), 0)
        migration_ok = storage_ok and migration_count >= len(ALL_MIGRATIONS)
        return [
            self._item(
                "storage.postgres",
                "passed" if storage_ok else "failed",
                "Primary storage is healthy PostgreSQL"
                if storage_ok
                else "Production must use a healthy PostgreSQL storage backend",
                detail={"type": info.get("type"), "db_type": db_type, "health": health.get("status")},
            ),
            self._item(
                "storage.migrations_applied",
                "passed" if migration_ok else "failed",
                "All known schema migrations are applied"
                if migration_ok
                else "Database schema migrations are missing or cannot be verified",
                detail={"applied": migration_count, "known": len(ALL_MIGRATIONS)},
            ),
        ]

    def _check_queue(self) -> list[dict[str, Any]]:
        try:
            service = self.image_job_service_factory()
            info = dict(service.queue_info())
        except Exception as exc:
            return [self._item("queue.redis", "failed", "Image job queue check failed", detail={"error": str(exc)})]

        backend = _clean(info.get("backend")).lower()
        stats_available = info.get("queued_count") is not None and info.get("dead_letter_count") is not None
        if backend != "redis":
            status = "failed"
            message = "Production image jobs must use Redis queue and distributed locks"
        elif not stats_available:
            status = "failed"
            message = "Redis queue is configured but Redis statistics are unavailable"
        else:
            status = "passed"
            message = "Image job queue uses Redis and exposes queue statistics"
        return [
            self._item(
                "queue.redis",
                status,
                message,
                detail={
                    "backend": backend,
                    "queued_count": info.get("queued_count"),
                    "dead_letter_count": info.get("dead_letter_count"),
                },
            )
        ]

    def _check_object_storage(self) -> list[dict[str, Any]]:
        configured_backend = _clean(self._env("OBJECT_STORAGE_BACKEND") or "local").lower()
        try:
            object_storage = self.object_storage_factory()
            info = dict(object_storage.info())
        except Exception as exc:
            return [self._item("object_storage.remote", "failed", "Object storage check failed", detail={"error": str(exc)})]

        remote_backends = {"s3", "r2", "minio", "oss", "cos"}
        remote_ok = configured_backend in remote_backends and _clean(info.get("backend")).lower() != "local"
        public_base_url = _clean(info.get("public_base_url") or self._env("OBJECT_STORAGE_PUBLIC_BASE_URL"))
        public_url_ok = public_base_url.startswith("https://")
        return [
            self._item(
                "object_storage.remote",
                "passed" if remote_ok else "failed",
                "Image assets use S3/R2/MinIO/OSS/COS-compatible object storage"
                if remote_ok
                else "Production image assets must not use local filesystem object storage",
                detail={"configured_backend": configured_backend, "runtime_backend": info.get("backend")},
            ),
            self._item(
                "object_storage.public_https_url",
                "passed" if public_url_ok else "failed",
                "OBJECT_STORAGE_PUBLIC_BASE_URL uses HTTPS"
                if public_url_ok
                else "OBJECT_STORAGE_PUBLIC_BASE_URL must be a public HTTPS URL",
                detail={"configured": bool(public_base_url)},
            ),
        ]

    def _check_backup_and_alerting(self) -> list[dict[str, Any]]:
        backup_dir = _clean(self._env("BACKUP_OUTPUT_DIR"))
        retention = _safe_int(self._env("BACKUP_RETENTION_DAYS"), 0)
        backup_ok = bool(backup_dir) and retention > 0
        alerts_ok = (
            _safe_int(self._env("ALERT_JOB_QUEUE_BACKLOG_THRESHOLD"), 0) > 0
            and _safe_int(self._env("ALERT_DISK_FREE_MB"), 0) > 0
            and _safe_int(self._env("ALERT_BACKUP_MAX_AGE_HOURS"), 0) > 0
        )
        return [
            self._item(
                "ops.backup_configured",
                "passed" if backup_ok else "warning",
                "Backup output and retention are configured"
                if backup_ok
                else "BACKUP_OUTPUT_DIR and BACKUP_RETENTION_DAYS should be configured before launch",
                detail={"backup_output_dir_configured": bool(backup_dir), "retention_days": retention},
            ),
            self._item(
                "ops.alert_thresholds",
                "passed" if alerts_ok else "warning",
                "Core alert thresholds are configured"
                if alerts_ok
                else "Queue, disk and backup alert thresholds should be configured before launch",
            ),
        ]


def _image_job_service():
    from services.image_job_service import image_job_service

    return image_job_service


production_readiness_service = ProductionReadinessService(
    config_obj=config,
    storage_factory=config.get_storage_backend,
    object_storage_factory=config.get_object_storage_backend,
    image_job_service_factory=_image_job_service,
)
