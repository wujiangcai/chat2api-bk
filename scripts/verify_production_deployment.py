#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

# Add project root to Python path when running from scripts/ directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.storage.migrations.versions import ALL_MIGRATIONS
from scripts.payment_webhook_sandbox import build_payload as build_webhook_payload
from scripts.payment_webhook_sandbox import canonical_body as canonical_webhook_body
from scripts.payment_webhook_sandbox import normalize_provider as normalize_webhook_provider
from scripts.payment_webhook_sandbox import sign_headers as sign_webhook_headers

TERMINAL_JOB_STATUSES = {"succeeded", "failed", "cancelled"}
REMOTE_OBJECT_STORAGE_BACKENDS = {"s3", "r2", "minio", "oss", "cos"}
REQUIRED_DEDICATED_COLLECTIONS = {
    "users",
    "packages",
    "cdks",
    "redemptions",
    "orders",
    "payments",
    "image_jobs",
    "image_assets",
    "audit_logs",
    "launch_evidence",
    "support_tickets",
    "auth_sessions",
    "auth_action_tokens",
}
REQUIRED_READINESS_ITEM_IDS = {
    "app.env.production",
    "app.base_url.https",
    "app.cors.public_https_origins",
    "app.security_headers",
    "app.auth_key.configured",
    "business.legal_identity",
    "payment.webhook_secret.configured",
    "payment.checkout.configured",
    "auth.cookie_session",
    "auth.email_verification_required",
    "auth.email_delivery.configured",
    "rate_limit.redis",
    "storage.postgres",
    "storage.migrations_applied",
    "queue.redis",
    "object_storage.remote",
    "object_storage.public_https_url",
}


@dataclass
class HttpResult:
    status_code: int
    body: Any = None
    text: str = ""
    headers: dict[str, str] | None = None
    latency_ms: int = 0
    error: str = ""


FetchFn = Callable[[str, str, dict[str, str], Any | None, float], HttpResult]


def _clean(value: object) -> str:
    return str(value or "").strip()


def _safe_len(value: object) -> int:
    return len(value) if isinstance(value, (list, tuple, dict, str)) else 0


def _is_https_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme == "https" and bool(parsed.netloc)


def _normalize_base_url(value: str) -> str:
    base_url = _clean(value).rstrip("/")
    if not base_url:
        raise ValueError("base url is required")
    parsed = urlsplit(base_url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("base url must include scheme and host")
    return base_url


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    redacted = {}
    for key, value in headers.items():
        redacted[key] = "[REDACTED]" if key.lower() == "authorization" else value
    return redacted


def default_fetch(method: str, url: str, headers: dict[str, str], body: Any | None, timeout: float) -> HttpResult:
    payload: bytes | None = None
    request_headers = dict(headers)
    if body is not None:
        if isinstance(body, bytes):
            payload = body
        else:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
    started = time.time()
    request = Request(url, data=payload, headers=request_headers, method=method.upper())
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - operator-provided URL for deployment verification
            raw = response.read()
            text = raw.decode("utf-8", errors="replace")
            response_headers = dict(response.headers.items())
            parsed_body: Any = None
            content_type = response_headers.get("Content-Type", response_headers.get("content-type", ""))
            if "json" in content_type.lower() or text.lstrip().startswith(("{", "[")):
                try:
                    parsed_body = json.loads(text)
                except json.JSONDecodeError:
                    parsed_body = None
            return HttpResult(
                status_code=int(response.status),
                body=parsed_body,
                text=text,
                headers=response_headers,
                latency_ms=int((time.time() - started) * 1000),
            )
    except HTTPError as exc:
        raw = exc.read()
        text = raw.decode("utf-8", errors="replace")
        parsed_body = None
        try:
            parsed_body = json.loads(text)
        except json.JSONDecodeError:
            pass
        return HttpResult(
            status_code=int(exc.code),
            body=parsed_body,
            text=text,
            headers=dict(exc.headers.items()) if exc.headers else {},
            latency_ms=int((time.time() - started) * 1000),
            error=str(exc),
        )
    except (OSError, URLError) as exc:
        return HttpResult(
            status_code=0,
            body=None,
            text="",
            headers={},
            latency_ms=int((time.time() - started) * 1000),
            error=str(exc),
        )


class ProductionDeploymentVerifier:
    def __init__(
        self,
        *,
        base_url: str,
        admin_key: str,
        fetch: FetchFn = default_fetch,
        timeout: float = 10.0,
        allow_http: bool = False,
    ):
        self.base_url = _normalize_base_url(base_url)
        self.admin_key = _clean(admin_key)
        self.fetch = fetch
        self.timeout = max(1.0, float(timeout or 10))
        self.allow_http = allow_http
        self._readiness_result: HttpResult | None = None
        self._storage_info_result: HttpResult | None = None

    def verify(
        self,
        *,
        run_image_job: bool = False,
        prompt: str = "production smoke test image",
        model: str = "gpt-image-2",
        poll_seconds: int = 180,
        poll_interval: float = 3.0,
        check_asset_url: bool = True,
        strict_launch: bool = False,
        run_payment_webhook_replay: bool = False,
        payment_webhook_provider: str = "stripe",
        payment_webhook_secret: str = "",
        payment_webhook_order_id: str = "",
        payment_webhook_amount_cents: int = 1990,
        payment_webhook_currency: str = "CNY",
        payment_webhook_refund: bool = True,
        run_checkout_initiation: bool = False,
        checkout_provider: str = "",
        checkout_amount_cents: int = 1990,
        checkout_currency: str = "CNY",
        checkout_quota: int = 1,
        run_checkout_webhook_replay: bool = False,
        checkout_webhook_provider: str = "stripe",
        checkout_webhook_secret: str = "",
        checkout_webhook_refund: bool = True,
    ) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        checks.append(self._check_base_url())
        checks.append(self._check_live())
        checks.append(self._check_security_headers())
        checks.append(self._check_ready())
        checks.append(self._check_production_readiness())
        checks.append(self._check_readiness_required_items())
        checks.append(self._check_auth_capabilities())
        checks.extend(self._check_storage_info())
        checks.append(self._check_prometheus_metrics())
        checks.append(self._check_alerts())
        checks.append(self._check_admin_assets_list())
        if run_image_job:
            checks.extend(
                self._check_image_job(
                    prompt=prompt,
                    model=model,
                    poll_seconds=poll_seconds,
                    poll_interval=poll_interval,
                    check_asset_url=check_asset_url,
                )
            )
        if run_payment_webhook_replay:
            checks.extend(
                self._check_payment_webhook_replay(
                    provider=payment_webhook_provider,
                    secret=payment_webhook_secret,
                    order_id=payment_webhook_order_id,
                    amount_cents=payment_webhook_amount_cents,
                    currency=payment_webhook_currency,
                    run_refund=payment_webhook_refund,
                )
            )
        if run_checkout_initiation or run_checkout_webhook_replay:
            checks.extend(
                self._check_checkout_initiation(
                    provider=checkout_provider,
                    amount_cents=checkout_amount_cents,
                    currency=checkout_currency,
                    quota=checkout_quota,
                    run_webhook_replay=run_checkout_webhook_replay,
                    webhook_provider=checkout_webhook_provider,
                    webhook_secret=checkout_webhook_secret,
                    webhook_refund=checkout_webhook_refund,
                )
            )
        if strict_launch:
            checks.append(self._check_strict_launch_e2e(run_image_job=run_image_job, check_asset_url=check_asset_url, checks=checks))
        failed = [item for item in checks if item["status"] == "failed"]
        warnings = [item for item in checks if item["status"] == "warning"]
        return {
            "status": "failed" if failed else "warning" if warnings else "passed",
            "ready": not failed,
            "strict_launch": strict_launch,
            "base_url": self.base_url,
            "ran_image_job": run_image_job,
            "ran_payment_webhook_replay": run_payment_webhook_replay,
            "ran_checkout_initiation": run_checkout_initiation or run_checkout_webhook_replay,
            "ran_checkout_webhook_replay": run_checkout_webhook_replay,
            "summary": {
                "total": len(checks),
                "passed": sum(1 for item in checks if item["status"] == "passed"),
                "warning": len(warnings),
                "failed": len(failed),
            },
            "evidence": self._evidence_summary(
                checks,
                ran_image_job=run_image_job,
                ran_payment_webhook_replay=run_payment_webhook_replay,
                ran_checkout_initiation=run_checkout_initiation or run_checkout_webhook_replay,
                ran_checkout_webhook_replay=run_checkout_webhook_replay,
            ),
            "checks": checks,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    def _url(self, path: str) -> str:
        return urljoin(f"{self.base_url}/", path.lstrip("/"))

    def _headers(self, *, admin: bool = False, token: str = "") -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        elif admin:
            headers["Authorization"] = f"Bearer {self.admin_key}"
        return headers

    def _request(
        self,
        method: str,
        path_or_url: str,
        *,
        admin: bool = False,
        token: str = "",
        body: Any | None = None,
        absolute: bool = False,
    ) -> HttpResult:
        url = path_or_url if absolute else self._url(path_or_url)
        headers = self._headers(admin=admin, token=token)
        return self.fetch(method.upper(), url, headers, body, self.timeout)

    def _request_with_headers(
        self,
        method: str,
        path_or_url: str,
        *,
        headers: dict[str, str],
        body: Any | None = None,
        absolute: bool = False,
    ) -> HttpResult:
        url = path_or_url if absolute else self._url(path_or_url)
        return self.fetch(method.upper(), url, headers, body, self.timeout)

    def _production_readiness_result(self) -> HttpResult:
        if self._readiness_result is None:
            self._readiness_result = self._request("GET", "/api/admin/production-readiness", admin=True)
        return self._readiness_result

    def _storage_info_result_cached(self) -> HttpResult:
        if self._storage_info_result is None:
            self._storage_info_result = self._request("GET", "/api/storage/info", admin=True)
        return self._storage_info_result

    @staticmethod
    def _check(check_id: str, passed: bool, message: str, *, detail: dict[str, Any] | None = None, warning: bool = False) -> dict[str, Any]:
        if passed:
            status = "passed"
        else:
            status = "warning" if warning else "failed"
        item: dict[str, Any] = {"id": check_id, "status": status, "message": message}
        if detail:
            item["detail"] = detail
        return item

    @staticmethod
    def _status_by_id(checks: list[dict[str, Any]]) -> dict[str, str]:
        return {str(item.get("id")): str(item.get("status")) for item in checks if isinstance(item, dict)}

    def _check_base_url(self) -> dict[str, Any]:
        ok = self.allow_http or _is_https_url(self.base_url)
        return self._check(
            "base_url.https",
            ok,
            "Base URL uses HTTPS" if ok else "Production base URL must use HTTPS",
            detail={"allow_http": self.allow_http},
        )

    def _check_live(self) -> dict[str, Any]:
        result = self._request("GET", "/health/live")
        body = result.body if isinstance(result.body, dict) else {}
        ok = result.status_code == 200 and body.get("status") == "ok"
        return self._check(
            "health.live",
            ok,
            "Live health check is ok" if ok else "Live health check failed",
            detail={"status_code": result.status_code, "latency_ms": result.latency_ms, "error": result.error or None},
        )

    def _check_security_headers(self) -> dict[str, Any]:
        result = self._request("GET", "/health/live")
        headers = {str(key).lower(): str(value) for key, value in (result.headers or {}).items()}
        required = {
            "strict-transport-security": bool(headers.get("strict-transport-security")),
            "x-content-type-options": headers.get("x-content-type-options", "").lower() == "nosniff",
            "x-frame-options": headers.get("x-frame-options", "").upper() in {"DENY", "SAMEORIGIN"},
            "content-security-policy": bool(headers.get("content-security-policy")),
            "referrer-policy": bool(headers.get("referrer-policy")),
        }
        missing = [name for name, present in required.items() if not present]
        ok = result.status_code == 200 and not missing
        return self._check(
            "security.headers",
            ok,
            "Security headers and HSTS are present on public responses" if ok else "Security headers or HSTS are missing on public responses",
            detail={
                "status_code": result.status_code,
                "missing": missing,
                "present": [name for name, present in required.items() if present],
            },
        )

    def _check_ready(self) -> dict[str, Any]:
        result = self._request("GET", "/health/ready")
        body = result.body if isinstance(result.body, dict) else {}
        ok = result.status_code == 200 and body.get("status") != "unhealthy"
        return self._check(
            "health.ready",
            ok,
            "Readiness check is healthy/degraded but serving" if ok else "Readiness check is unhealthy",
            detail={"status_code": result.status_code, "status": body.get("status"), "latency_ms": result.latency_ms},
        )

    def _check_production_readiness(self) -> dict[str, Any]:
        result = self._production_readiness_result()
        body = result.body if isinstance(result.body, dict) else {}
        ok = result.status_code == 200 and bool(body.get("ready")) and body.get("status") in {"passed", "warning"}
        failed_items = [
            {"id": item.get("id"), "message": item.get("message")}
            for item in body.get("items", [])
            if isinstance(item, dict) and item.get("status") == "failed"
        ][:10]
        return self._check(
            "admin.production_readiness",
            ok,
            "Production readiness preflight passed" if ok else "Production readiness preflight failed",
            detail={
                "status_code": result.status_code,
                "status": body.get("status"),
                "ready": body.get("ready"),
                "summary": body.get("summary"),
                "failed_items": failed_items,
            },
        )

    def _check_readiness_required_items(self) -> dict[str, Any]:
        result = self._production_readiness_result()
        body = result.body if isinstance(result.body, dict) else {}
        items = body.get("items") if isinstance(body.get("items"), list) else []
        item_by_id = {str(item.get("id")): item for item in items if isinstance(item, dict)}
        missing = sorted(check_id for check_id in REQUIRED_READINESS_ITEM_IDS if check_id not in item_by_id)
        not_passed = sorted(
            {
                check_id: str(item_by_id[check_id].get("status"))
                for check_id in REQUIRED_READINESS_ITEM_IDS
                if check_id in item_by_id and item_by_id[check_id].get("status") != "passed"
            }.items()
        )
        ok = result.status_code == 200 and not missing and not not_passed
        return self._check(
            "admin.production_readiness.required_items",
            ok,
            "All launch-critical production readiness checks are passed"
            if ok
            else "Some launch-critical production readiness checks are missing or not passed",
            detail={
                "status_code": result.status_code,
                "missing": missing,
                "not_passed": [{"id": check_id, "status": status} for check_id, status in not_passed],
            },
        )

    def _check_auth_capabilities(self) -> dict[str, Any]:
        result = self._request("GET", "/auth/capabilities")
        body = result.body if isinstance(result.body, dict) else {}
        provider = _clean(body.get("email_provider")).lower()
        requirements = {
            "session_cookie_enabled": body.get("session_cookie_enabled") is True,
            "email_verification_required": body.get("email_verification_required") is True,
            "password_reset_enabled": body.get("password_reset_enabled") is True,
            "email_delivery_configured": body.get("email_delivery_configured") is True and provider not in {"", "console", "disabled"},
        }
        missing = [name for name, passed in requirements.items() if not passed]
        ok = result.status_code == 200 and not missing
        return self._check(
            "auth.capabilities.public_accounts",
            ok,
            "Public account capabilities are production-ready" if ok else "Public account capabilities are not production-ready",
            detail={
                "status_code": result.status_code,
                "missing": missing,
                "email_provider": provider or None,
                "registration_enabled": body.get("registration_enabled"),
            },
        )

    def _check_storage_info(self) -> list[dict[str, Any]]:
        result = self._storage_info_result_cached()
        body = result.body if isinstance(result.body, dict) else {}
        backend = body.get("backend") if isinstance(body.get("backend"), dict) else {}
        health = body.get("health") if isinstance(body.get("health"), dict) else {}
        object_storage = body.get("object_storage") if isinstance(body.get("object_storage"), dict) else {}
        queue = body.get("image_job_queue") if isinstance(body.get("image_job_queue"), dict) else {}
        rate_limit = body.get("rate_limit") if isinstance(body.get("rate_limit"), dict) else {}

        storage_ok = result.status_code == 200 and backend.get("type") == "database" and backend.get("db_type") == "postgresql" and health.get("status") == "healthy"
        migration_count = int(health.get("schema_migration_count") or 0) if str(health.get("schema_migration_count") or "0").isdigit() else 0
        migrations_ok = storage_ok and migration_count >= len(ALL_MIGRATIONS)
        dedicated_counts = health.get("dedicated_collection_counts") if isinstance(health.get("dedicated_collection_counts"), dict) else {}
        dedicated_missing = sorted(name for name in REQUIRED_DEDICATED_COLLECTIONS if name not in dedicated_counts)
        quota_ledger_visible = "quota_ledger_count" in health
        dedicated_ok = storage_ok and not dedicated_missing and quota_ledger_visible
        object_backend = _clean(object_storage.get("backend")).lower()
        public_base_url = _clean(object_storage.get("public_base_url"))
        object_ok = result.status_code == 200 and object_backend in REMOTE_OBJECT_STORAGE_BACKENDS
        object_public_ok = object_ok and _is_https_url(public_base_url)
        queue_ok = result.status_code == 200 and _clean(queue.get("backend")).lower() == "redis"
        public_limiter = rate_limit.get("public_actions") if isinstance(rate_limit.get("public_actions"), dict) else {}
        api_limiter = rate_limit.get("api_keys") if isinstance(rate_limit.get("api_keys"), dict) else {}
        rate_limit_ok = (
            result.status_code == 200
            and _clean(public_limiter.get("backend")).lower() == "redis"
            and _clean(api_limiter.get("backend")).lower() == "redis"
            and public_limiter.get("healthy") is not False
            and api_limiter.get("healthy") is not False
        )
        return [
            self._check(
                "storage.postgresql",
                storage_ok,
                "Storage backend is healthy PostgreSQL" if storage_ok else "Storage backend is not healthy PostgreSQL",
                detail={"status_code": result.status_code, "backend": backend, "health_status": health.get("status")},
            ),
            self._check(
                "storage.migrations_applied",
                migrations_ok,
                "All known database migrations are applied" if migrations_ok else "Database migrations are missing or cannot be verified",
                detail={"applied": migration_count, "known": len(ALL_MIGRATIONS)},
            ),
            self._check(
                "storage.dedicated_tables",
                dedicated_ok,
                "Commercial/account dedicated tables are visible in database health"
                if dedicated_ok
                else "Commercial/account dedicated table evidence is missing from database health",
                detail={
                    "missing": dedicated_missing,
                    "quota_ledger_visible": quota_ledger_visible,
                    "known_count": len(REQUIRED_DEDICATED_COLLECTIONS) + 1,
                },
            ),
            self._check(
                "object_storage.remote",
                object_ok,
                "Object storage backend is remote/S3-compatible" if object_ok else "Object storage backend is not remote/S3-compatible",
                detail={"backend": object_storage.get("backend"), "public_base_url": object_storage.get("public_base_url")},
            ),
            self._check(
                "object_storage.public_https_url",
                object_public_ok,
                "Object storage public base URL is HTTPS"
                if object_public_ok
                else "Object storage public base URL is missing or not HTTPS",
                detail={"public_base_url": public_base_url or None},
            ),
            self._check(
                "queue.redis",
                queue_ok,
                "Image job queue backend is Redis" if queue_ok else "Image job queue backend is not Redis",
                detail={"backend": queue.get("backend"), "queued_count": queue.get("queued_count"), "dead_letter_count": queue.get("dead_letter_count")},
            ),
            self._check(
                "rate_limit.redis_runtime",
                rate_limit_ok,
                "Public and API key rate limiters use Redis"
                if rate_limit_ok
                else "Public/API key rate limiters are not both Redis-backed at runtime",
                detail={
                    "public_actions_backend": public_limiter.get("backend"),
                    "api_keys_backend": api_limiter.get("backend"),
                },
            ),
        ]

    def _check_prometheus_metrics(self) -> dict[str, Any]:
        result = self._request("GET", "/api/admin/metrics?format=prometheus", admin=True)
        ok = result.status_code == 200 and "chatgpt2api_up 1" in result.text
        return self._check(
            "admin.metrics.prometheus",
            ok,
            "Prometheus metrics are available and service is up" if ok else "Prometheus metrics are unavailable or report service down",
            detail={"status_code": result.status_code, "latency_ms": result.latency_ms},
        )

    def _check_alerts(self) -> dict[str, Any]:
        result = self._request("GET", "/api/admin/alerts", admin=True)
        body = result.body if isinstance(result.body, dict) else {}
        alerts = body.get("alerts") if isinstance(body.get("alerts"), list) else []
        critical = [item for item in alerts if isinstance(item, dict) and item.get("severity") == "critical"]
        ok = result.status_code == 200 and not critical
        return self._check(
            "admin.alerts.no_critical",
            ok,
            "No critical alerts are active" if ok else "Critical alerts are active",
            detail={"status_code": result.status_code, "alert_count": len(alerts), "critical_count": len(critical), "alerts": alerts[:10]},
        )

    def _check_admin_assets_list(self) -> dict[str, Any]:
        result = self._request("GET", "/api/admin/assets?limit=1", admin=True)
        body = result.body if isinstance(result.body, dict) else {}
        items = body.get("items") if isinstance(body.get("items"), list) else []
        ok = result.status_code == 200
        return self._check(
            "admin.assets.list",
            ok,
            "Admin asset listing is available" if ok else "Admin asset listing failed",
            detail={"status_code": result.status_code, "asset_sample_count": len(items)},
        )

    def _check_image_job(
        self,
        *,
        prompt: str,
        model: str,
        poll_seconds: int,
        poll_interval: float,
        check_asset_url: bool,
    ) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []
        enqueue = self._request(
            "POST",
            "/api/jobs/images/generations",
            admin=True,
            body={"prompt": prompt, "model": model, "n": 1, "response_format": "url"},
        )
        enqueue_body = enqueue.body if isinstance(enqueue.body, dict) else {}
        job = enqueue_body.get("job") if isinstance(enqueue_body.get("job"), dict) else {}
        job_id = _clean(job.get("id"))
        enqueue_ok = enqueue.status_code in {200, 202} and bool(job_id)
        checks.append(
            self._check(
                "image_job.enqueue",
                enqueue_ok,
                "Async image job was enqueued" if enqueue_ok else "Async image job enqueue failed",
                detail={"status_code": enqueue.status_code, "job_id": job_id or None, "error": enqueue.error or None},
            )
        )
        if not enqueue_ok:
            return checks

        deadline = time.time() + max(1, int(poll_seconds or 180))
        final_job: dict[str, Any] = {}
        poll_count = 0
        while time.time() <= deadline:
            poll_count += 1
            current = self._request("GET", f"/api/jobs/{job_id}", admin=True)
            current_body = current.body if isinstance(current.body, dict) else {}
            final_job = current_body.get("job") if isinstance(current_body.get("job"), dict) else {}
            if final_job.get("status") in TERMINAL_JOB_STATUSES:
                break
            time.sleep(max(0.2, float(poll_interval or 3)))

        status = _clean(final_job.get("status"))
        succeeded = status == "succeeded"
        checks.append(
            self._check(
                "image_job.succeeded",
                succeeded,
                "Async image job succeeded" if succeeded else "Async image job did not succeed before timeout",
                detail={"job_id": job_id, "status": status or None, "poll_count": poll_count, "assets": _safe_len(final_job.get("assets"))},
            )
        )
        if not succeeded:
            return checks

        asset_url = self._first_asset_url(final_job)
        has_asset = bool(asset_url)
        checks.append(
            self._check(
                "image_job.asset_recorded",
                has_asset,
                "Image job recorded an asset URL" if has_asset else "Image job succeeded but no asset URL was recorded",
                detail={"job_id": job_id, "asset_url": asset_url or None},
            )
        )
        if check_asset_url and asset_url:
            result = self._request("GET", asset_url, absolute=True)
            public_ok = 200 <= result.status_code < 400
            checks.append(
                self._check(
                    "image_job.asset_url_public",
                    public_ok,
                    "Generated asset URL is publicly reachable" if public_ok else "Generated asset URL is not publicly reachable",
                    detail={"status_code": result.status_code, "latency_ms": result.latency_ms, "url": asset_url},
                )
            )
        return checks

    def _check_payment_webhook_replay(
        self,
        *,
        provider: str,
        secret: str,
        order_id: str,
        amount_cents: int,
        currency: str,
        run_refund: bool,
    ) -> list[dict[str, Any]]:
        return self._replay_signed_payment_webhooks(
            check_prefix="payment_webhook.replay",
            provider=provider,
            secret=secret,
            order_id=order_id,
            amount_cents=amount_cents,
            currency=currency,
            run_refund=run_refund,
            configured_message="Payment webhook replay inputs are configured",
            configured_error="Payment webhook replay requires --payment-webhook-secret and --payment-webhook-order-id",
            paid_success="Signed paid webhook replay fulfilled the target order",
            paid_error="Signed paid webhook replay did not fulfill the target order",
            refund_success="Signed refund webhook replay refunded the target order",
            refund_error="Signed refund webhook replay did not refund the target order",
        )

    def _replay_signed_payment_webhooks(
        self,
        *,
        check_prefix: str,
        provider: str,
        secret: str,
        order_id: str,
        amount_cents: int,
        currency: str,
        run_refund: bool,
        configured_message: str,
        configured_error: str,
        paid_success: str,
        paid_error: str,
        refund_success: str,
        refund_error: str,
        extra_detail: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []
        normalized_provider = normalize_webhook_provider(provider or "stripe")
        normalized_secret = _clean(secret)
        normalized_order_id = _clean(order_id)
        detail_context = dict(extra_detail or {})
        config_ok = bool(normalized_secret and normalized_order_id)
        checks.append(
            self._check(
                f"{check_prefix}.configured",
                config_ok,
                configured_message if config_ok else configured_error,
                detail={
                    "provider": normalized_provider,
                    "order_id": normalized_order_id or None,
                    "has_secret": bool(normalized_secret),
                    "run_refund": run_refund,
                    **detail_context,
                },
            )
        )
        if not config_ok:
            return checks

        paid_payload = build_webhook_payload(
            provider=normalized_provider,
            action="paid",
            order_id=normalized_order_id,
            amount_cents=amount_cents,
            currency=currency,
            provider_payment_id=f"verify_paid_{normalized_order_id}_{int(time.time())}",
            event_id=f"evt_verify_paid_{int(time.time())}",
        )
        paid_body = canonical_webhook_body(paid_payload)
        paid_headers = sign_webhook_headers(provider=normalized_provider, secret=normalized_secret, body=paid_body)
        paid_result = self._request_with_headers(
            "POST",
            f"/api/payments/webhook/{normalized_provider}",
            headers=paid_headers,
            body=paid_body,
        )
        paid_response = paid_result.body if isinstance(paid_result.body, dict) else {}
        paid_order = paid_response.get("order") if isinstance(paid_response.get("order"), dict) else {}
        paid_payment = paid_response.get("payment") if isinstance(paid_response.get("payment"), dict) else {}
        paid_ok = (
            paid_result.status_code == 200
            and paid_response.get("ok") is True
            and paid_response.get("ignored") is False
            and paid_response.get("action") == "mark_paid"
            and paid_order.get("id") == normalized_order_id
            and paid_order.get("status") in {"paid", "fulfilled"}
            and paid_payment.get("status") == "succeeded"
        )
        checks.append(
            self._check(
                f"{check_prefix}.paid",
                paid_ok,
                paid_success if paid_ok else paid_error,
                detail={
                    "status_code": paid_result.status_code,
                    "provider": normalized_provider,
                    "order_id": normalized_order_id,
                    "response_action": paid_response.get("action"),
                    "order_status": paid_order.get("status"),
                    "payment_status": paid_payment.get("status"),
                    "error": paid_result.error or None,
                    **detail_context,
                },
            )
        )
        if not run_refund:
            return checks
        if not paid_ok:
            checks.append(
                self._check(
                    f"{check_prefix}.refund",
                    False,
                    "Signed refund webhook replay skipped because paid replay failed",
                    detail={"provider": normalized_provider, "order_id": normalized_order_id, **detail_context},
                )
            )
            return checks

        refund_payload = build_webhook_payload(
            provider=normalized_provider,
            action="refund",
            order_id=normalized_order_id,
            amount_cents=amount_cents,
            currency=currency,
            provider_payment_id=str(paid_payment.get("provider_payment_id") or paid_payment.get("id") or f"verify_paid_{normalized_order_id}"),
            event_id=f"evt_verify_refund_{int(time.time())}",
        )
        refund_body = canonical_webhook_body(refund_payload)
        refund_headers = sign_webhook_headers(provider=normalized_provider, secret=normalized_secret, body=refund_body)
        refund_result = self._request_with_headers(
            "POST",
            f"/api/payments/webhook/{normalized_provider}",
            headers=refund_headers,
            body=refund_body,
        )
        refund_response = refund_result.body if isinstance(refund_result.body, dict) else {}
        refund_order = refund_response.get("order") if isinstance(refund_response.get("order"), dict) else {}
        refund_payment = refund_response.get("payment") if isinstance(refund_response.get("payment"), dict) else {}
        refund_ok = (
            refund_result.status_code == 200
            and refund_response.get("ok") is True
            and refund_response.get("ignored") is False
            and refund_response.get("action") == "refund"
            and refund_order.get("id") == normalized_order_id
            and refund_order.get("status") == "refunded"
            and refund_payment.get("status") == "refunded"
        )
        checks.append(
            self._check(
                f"{check_prefix}.refund",
                refund_ok,
                refund_success if refund_ok else refund_error,
                detail={
                    "status_code": refund_result.status_code,
                    "provider": normalized_provider,
                    "order_id": normalized_order_id,
                    "response_action": refund_response.get("action"),
                    "order_status": refund_order.get("status"),
                    "payment_status": refund_payment.get("status"),
                    "quota_deducted": refund_response.get("quota_deducted"),
                    "error": refund_result.error or None,
                    **detail_context,
                },
            )
        )
        return checks

    def _check_checkout_initiation(
        self,
        *,
        provider: str,
        amount_cents: int,
        currency: str,
        quota: int,
        run_webhook_replay: bool,
        webhook_provider: str,
        webhook_secret: str,
        webhook_refund: bool,
    ) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []
        run_id = uuid.uuid4().hex[:10]
        safe_amount = max(1, int(amount_cents or 1990))
        safe_quota = max(1, int(quota or 1))
        safe_currency = _clean(currency).upper() or "CNY"
        normalized_provider = _clean(provider)
        package_id = ""
        user_id = ""
        user_token = ""
        order_id = ""
        cleanup_checks: list[dict[str, Any]] = []

        try:
            package_result = self._request(
                "POST",
                "/api/admin/packages",
                admin=True,
                body={
                    "name": f"Launch checkout verification {run_id}",
                    "description": "Temporary package created by verify_production_deployment.py",
                    "quota": safe_quota,
                    "price_cents": safe_amount,
                    "currency": safe_currency,
                    "valid_days": 1,
                },
            )
            package_body = package_result.body if isinstance(package_result.body, dict) else {}
            package = package_body.get("item") if isinstance(package_body.get("item"), dict) else {}
            package_id = _clean(package.get("id"))

            password = f"Verify-{run_id}-Pass123"
            user_result = self._request(
                "POST",
                "/api/admin/users",
                admin=True,
                body={
                    "email": f"launch-checkout-{run_id}@example.invalid",
                    "password": password,
                    "name": f"Launch Checkout {run_id}",
                    "quota_balance": 0,
                },
            )
            user_body = user_result.body if isinstance(user_result.body, dict) else {}
            user = user_body.get("item") if isinstance(user_body.get("item"), dict) else {}
            user_id = _clean(user.get("id"))
            user_token = _clean(user_body.get("token"))

            fixtures_ok = (
                200 <= package_result.status_code < 300
                and bool(package_id)
                and 200 <= user_result.status_code < 300
                and bool(user_id)
                and bool(user_token)
            )
            checks.append(
                self._check(
                    "payment_checkout.fixtures",
                    fixtures_ok,
                    "Disposable package and user were created for checkout verification"
                    if fixtures_ok
                    else "Could not create disposable package/user for checkout verification",
                    detail={
                        "package_status_code": package_result.status_code,
                        "package_id": package_id or None,
                        "user_status_code": user_result.status_code,
                        "user_id": user_id or None,
                        "user_token_returned": bool(user_token),
                        "package_error": package_result.error or None,
                        "user_error": user_result.error or None,
                    },
                )
            )
            if not fixtures_ok:
                return checks

            order_result = self._request(
                "POST",
                "/api/orders",
                token=user_token,
                body={
                    "package_id": package_id,
                    "quantity": 1,
                    "metadata": {"source": "remote-verifier", "run_id": run_id},
                },
            )
            order_body = order_result.body if isinstance(order_result.body, dict) else {}
            order = order_body.get("order") if isinstance(order_body.get("order"), dict) else {}
            order_id = _clean(order.get("id"))
            order_ok = (
                200 <= order_result.status_code < 300
                and bool(order_id)
                and order.get("status") in {"created", "pending_payment"}
                and int(order.get("amount_cents") or 0) == safe_amount
            )
            checks.append(
                self._check(
                    "payment_checkout.order_created",
                    order_ok,
                    "Disposable order was created through the user order API"
                    if order_ok
                    else "Disposable order could not be created through the user order API",
                    detail={
                        "status_code": order_result.status_code,
                        "order_id": order_id or None,
                        "order_status": order.get("status"),
                        "amount_cents": order.get("amount_cents"),
                        "currency": order.get("currency"),
                        "error": order_result.error or None,
                    },
                )
            )
            if not order_ok:
                return checks

            checkout_body: dict[str, Any] = {
                "metadata": {"source": "remote-verifier", "run_id": run_id},
            }
            if normalized_provider:
                checkout_body["provider"] = normalized_provider
            checkout_result = self._request(
                "POST",
                f"/api/orders/{order_id}/checkout",
                token=user_token,
                body=checkout_body,
            )
            checkout_response = checkout_result.body if isinstance(checkout_result.body, dict) else {}
            checkout = checkout_response.get("checkout") if isinstance(checkout_response.get("checkout"), dict) else {}
            checkout_order = checkout_response.get("order") if isinstance(checkout_response.get("order"), dict) else {}
            checkout_provider = _clean(checkout.get("provider"))
            payment_url = _clean(checkout.get("payment_url"))
            instructions = _clean(checkout.get("instructions"))
            mode = _clean(checkout.get("mode"))
            has_payable_surface = bool(payment_url or instructions)
            if checkout_provider in {"redirect", "stripe"}:
                has_payable_surface = _is_https_url(payment_url)
            checkout_ok = (
                200 <= checkout_result.status_code < 300
                and bool(checkout.get("id"))
                and checkout.get("order_id") == order_id
                and int(checkout.get("amount_cents") or 0) == safe_amount
                and checkout_order.get("id") == order_id
                and has_payable_surface
            )
            checks.append(
                self._check(
                    "payment_checkout.session_created",
                    checkout_ok,
                    "Checkout initiation returned a payable session/link"
                    if checkout_ok
                    else "Checkout initiation did not return a payable session/link",
                    detail={
                        "status_code": checkout_result.status_code,
                        "provider": checkout_provider or None,
                        "requested_provider": normalized_provider or None,
                        "mode": mode or None,
                        "order_id": order_id,
                        "checkout_id": checkout.get("id"),
                        "provider_session_id": checkout.get("provider_session_id"),
                        "payment_url_https": _is_https_url(payment_url) if payment_url else False,
                        "has_instructions": bool(instructions),
                        "order_metadata_checkout_id": (
                            checkout_order.get("metadata", {}).get("checkout", {}).get("id")
                            if isinstance(checkout_order.get("metadata"), dict)
                            and isinstance(checkout_order.get("metadata", {}).get("checkout"), dict)
                            else None
                        ),
                        "error": checkout_result.error or None,
                    },
                )
            )
            if run_webhook_replay:
                if checkout_ok:
                    checks.extend(
                        self._replay_signed_payment_webhooks(
                            check_prefix="payment_checkout.webhook_replay",
                            provider=webhook_provider,
                            secret=webhook_secret,
                            order_id=order_id,
                            amount_cents=safe_amount,
                            currency=safe_currency,
                            run_refund=webhook_refund,
                            configured_message="Checkout order webhook replay inputs are configured",
                            configured_error="Checkout webhook replay requires --checkout-webhook-secret or --payment-webhook-secret",
                            paid_success="Signed paid webhook replay fulfilled the checkout order",
                            paid_error="Signed paid webhook replay did not fulfill the checkout order",
                            refund_success="Signed refund webhook replay refunded the checkout order",
                            refund_error="Signed refund webhook replay did not refund the checkout order",
                            extra_detail={
                                "checkout_id": checkout.get("id"),
                                "checkout_provider": checkout_provider or None,
                                "run_id": run_id,
                            },
                        )
                    )
                else:
                    checks.append(
                        self._check(
                            "payment_checkout.webhook_replay.paid",
                            False,
                            "Checkout webhook replay skipped because checkout initiation failed",
                            detail={"order_id": order_id or None, "run_id": run_id},
                        )
                    )
        finally:
            if package_id:
                package_cleanup = self._request("POST", f"/api/admin/packages/{package_id}", admin=True, body={"enabled": False})
                cleanup_checks.append(
                    self._check(
                        "payment_checkout.cleanup.package_disabled",
                        200 <= package_cleanup.status_code < 300,
                        "Temporary checkout package was disabled"
                        if 200 <= package_cleanup.status_code < 300
                        else "Temporary checkout package could not be disabled",
                        detail={"package_id": package_id, "status_code": package_cleanup.status_code},
                        warning=True,
                    )
                )
            if user_id:
                user_cleanup = self._request("POST", f"/api/admin/users/{user_id}", admin=True, body={"enabled": False})
                cleanup_checks.append(
                    self._check(
                        "payment_checkout.cleanup.user_disabled",
                        200 <= user_cleanup.status_code < 300,
                        "Temporary checkout user was disabled"
                        if 200 <= user_cleanup.status_code < 300
                        else "Temporary checkout user could not be disabled",
                        detail={"user_id": user_id, "status_code": user_cleanup.status_code},
                        warning=True,
                    )
                )
            checks.extend(cleanup_checks)
        return checks

    def _check_strict_launch_e2e(
        self,
        *,
        run_image_job: bool,
        check_asset_url: bool,
        checks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        status_by_id = self._status_by_id(checks)
        required = {
            "image_job_requested": run_image_job,
            "image_job.succeeded": status_by_id.get("image_job.succeeded") == "passed",
            "image_job.asset_recorded": status_by_id.get("image_job.asset_recorded") == "passed",
            "image_job.asset_url_public": check_asset_url and status_by_id.get("image_job.asset_url_public") == "passed",
        }
        missing = [name for name, passed in required.items() if not passed]
        ok = not missing
        return self._check(
            "launch.strict_image_pipeline_e2e",
            ok,
            "Strict launch mode has public image-job/object-storage end-to-end evidence"
            if ok
            else "Strict launch mode requires --image-job with a public generated asset URL",
            detail={"missing": missing, "check_asset_url": check_asset_url},
        )

    @staticmethod
    def _evidence_summary(
        checks: list[dict[str, Any]],
        *,
        ran_image_job: bool,
        ran_payment_webhook_replay: bool,
        ran_checkout_initiation: bool,
        ran_checkout_webhook_replay: bool,
    ) -> dict[str, Any]:
        status_by_id = ProductionDeploymentVerifier._status_by_id(checks)

        def passed(check_id: str) -> bool:
            return status_by_id.get(check_id) == "passed"

        return {
            "https": passed("base_url.https"),
            "security_headers": passed("security.headers"),
            "production_readiness": passed("admin.production_readiness"),
            "readiness_required_items": passed("admin.production_readiness.required_items"),
            "postgresql": passed("storage.postgresql"),
            "database_migrations": passed("storage.migrations_applied"),
            "dedicated_tables": passed("storage.dedicated_tables"),
            "redis_queue": passed("queue.redis"),
            "redis_rate_limit": passed("rate_limit.redis_runtime"),
            "remote_object_storage": passed("object_storage.remote"),
            "object_storage_public_https": passed("object_storage.public_https_url"),
            "auth_cookie_email_recovery": passed("auth.capabilities.public_accounts"),
            "prometheus_metrics": passed("admin.metrics.prometheus"),
            "no_critical_alerts": passed("admin.alerts.no_critical"),
            "image_job_e2e_requested": ran_image_job,
            "image_job_e2e_succeeded": passed("image_job.succeeded"),
            "generated_asset_public_url": passed("image_job.asset_url_public"),
            "payment_webhook_replay_requested": ran_payment_webhook_replay,
            "payment_webhook_paid_replay": passed("payment_webhook.replay.paid"),
            "payment_webhook_refund_replay": passed("payment_webhook.replay.refund"),
            "payment_checkout_initiation_requested": ran_checkout_initiation,
            "payment_checkout_order_created": passed("payment_checkout.order_created"),
            "payment_checkout_session_created": passed("payment_checkout.session_created"),
            "payment_checkout_webhook_replay_requested": ran_checkout_webhook_replay,
            "payment_checkout_paid_replay": passed("payment_checkout.webhook_replay.paid"),
            "payment_checkout_refund_replay": passed("payment_checkout.webhook_replay.refund"),
            "launch_evidence_strict_ready": (
                passed("base_url.https")
                and passed("security.headers")
                and passed("admin.production_readiness.required_items")
                and passed("storage.postgresql")
                and passed("storage.migrations_applied")
                and passed("storage.dedicated_tables")
                and passed("queue.redis")
                and passed("rate_limit.redis_runtime")
                and passed("object_storage.remote")
                and passed("object_storage.public_https_url")
                and passed("auth.capabilities.public_accounts")
                and passed("admin.metrics.prometheus")
                and passed("admin.alerts.no_critical")
                and ran_image_job
                and passed("image_job.succeeded")
                and passed("image_job.asset_url_public")
            ),
        }

    @staticmethod
    def _first_asset_url(job: dict[str, Any]) -> str:
        assets = job.get("assets") if isinstance(job.get("assets"), list) else []
        for asset in assets:
            if isinstance(asset, dict):
                url = _clean(asset.get("url") or asset.get("public_url"))
                if url:
                    return url
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        data = result.get("data") if isinstance(result.get("data"), list) else []
        for item in data:
            if isinstance(item, dict):
                url = _clean(item.get("url"))
                if url:
                    return url
        return ""


def verify_deployment(
    *,
    base_url: str,
    admin_key: str,
    fetch: FetchFn = default_fetch,
    timeout: float = 10.0,
    allow_http: bool = False,
    run_image_job: bool = False,
    prompt: str = "production smoke test image",
    model: str = "gpt-image-2",
    poll_seconds: int = 180,
    poll_interval: float = 3.0,
    check_asset_url: bool = True,
    strict_launch: bool = False,
    run_payment_webhook_replay: bool = False,
    payment_webhook_provider: str = "stripe",
    payment_webhook_secret: str = "",
    payment_webhook_order_id: str = "",
    payment_webhook_amount_cents: int = 1990,
    payment_webhook_currency: str = "CNY",
    payment_webhook_refund: bool = True,
    run_checkout_initiation: bool = False,
    checkout_provider: str = "",
    checkout_amount_cents: int = 1990,
    checkout_currency: str = "CNY",
    checkout_quota: int = 1,
    run_checkout_webhook_replay: bool = False,
    checkout_webhook_provider: str = "stripe",
    checkout_webhook_secret: str = "",
    checkout_webhook_refund: bool = True,
) -> dict[str, Any]:
    verifier = ProductionDeploymentVerifier(
        base_url=base_url,
        admin_key=admin_key,
        fetch=fetch,
        timeout=timeout,
        allow_http=allow_http,
    )
    return verifier.verify(
        run_image_job=run_image_job,
        prompt=prompt,
        model=model,
        poll_seconds=poll_seconds,
        poll_interval=poll_interval,
        check_asset_url=check_asset_url,
        strict_launch=strict_launch,
        run_payment_webhook_replay=run_payment_webhook_replay,
        payment_webhook_provider=payment_webhook_provider,
        payment_webhook_secret=payment_webhook_secret,
        payment_webhook_order_id=payment_webhook_order_id,
        payment_webhook_amount_cents=payment_webhook_amount_cents,
        payment_webhook_currency=payment_webhook_currency,
        payment_webhook_refund=payment_webhook_refund,
        run_checkout_initiation=run_checkout_initiation,
        checkout_provider=checkout_provider,
        checkout_amount_cents=checkout_amount_cents,
        checkout_currency=checkout_currency,
        checkout_quota=checkout_quota,
        run_checkout_webhook_replay=run_checkout_webhook_replay,
        checkout_webhook_provider=checkout_webhook_provider,
        checkout_webhook_secret=checkout_webhook_secret,
        checkout_webhook_refund=checkout_webhook_refund,
    )


def upload_launch_evidence(
    *,
    base_url: str,
    admin_key: str,
    report: dict[str, Any],
    name: str = "",
    source: str = "remote-verifier",
    fetch: FetchFn = default_fetch,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Upload a verifier report to the deployed service's launch evidence archive."""

    normalized_base_url = _normalize_base_url(base_url)
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {_clean(admin_key)}",
    }
    result = fetch(
        "POST",
        urljoin(f"{normalized_base_url}/", "/api/admin/launch-evidence".lstrip("/")),
        headers,
        {
            "name": name or f"launch evidence {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}",
            "source": source or "remote-verifier",
            "report": report,
        },
        max(1.0, float(timeout or 10)),
    )
    body = result.body if isinstance(result.body, dict) else {}
    item = body.get("item") if isinstance(body.get("item"), dict) else {}
    ok = 200 <= result.status_code < 300 and bool(item.get("id"))
    return {
        "ok": ok,
        "status": "uploaded" if ok else "failed",
        "status_code": result.status_code,
        "evidence_id": item.get("id"),
        "error": result.error or (body.get("error") if isinstance(body.get("error"), str) else ""),
    }


def _print_human(result: dict[str, Any]) -> None:
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    print(
        f"Production deployment verification: {result.get('status')} "
        f"({summary.get('passed', 0)} passed, {summary.get('warning', 0)} warnings, {summary.get('failed', 0)} failed)"
    )
    print(f"Base URL: {result.get('base_url')}")
    evidence = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
    if evidence:
        strict_ready = "yes" if evidence.get("launch_evidence_strict_ready") else "no"
        print(f"Strict launch evidence complete: {strict_ready}")
    for item in result.get("checks") or []:
        if not isinstance(item, dict):
            continue
        marker = {"passed": "OK", "warning": "WARN", "failed": "FAIL"}.get(str(item.get("status")), "INFO")
        print(f"- [{marker}] {item.get('id')}: {item.get('message')}")
    upload = result.get("launch_evidence_upload")
    if isinstance(upload, dict):
        marker = "OK" if upload.get("ok") else "FAIL"
        suffix = f" ({upload.get('evidence_id')})" if upload.get("evidence_id") else ""
        print(f"- [{marker}] launch_evidence_upload: {upload.get('status')}{suffix}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a deployed commercial chatgpt2api service and write launch evidence.")
    parser.add_argument("--base-url", required=True, help="Public deployment base URL, e.g. https://img.example.com")
    parser.add_argument("--admin-key", required=True, help="Admin API key/token used for admin verification endpoints.")
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout seconds per request.")
    parser.add_argument("--allow-http", action="store_true", help="Allow http:// base URLs for staging-only verification.")
    parser.add_argument("--image-job", action="store_true", help="Run an optional end-to-end async image job smoke test.")
    parser.add_argument("--prompt", default="production smoke test image", help="Prompt used when --image-job is enabled.")
    parser.add_argument("--model", default="gpt-image-2", help="Image model used when --image-job is enabled.")
    parser.add_argument("--poll-seconds", type=int, default=180, help="Max seconds to wait for the optional image job.")
    parser.add_argument("--poll-interval", type=float, default=3.0, help="Polling interval for the optional image job.")
    parser.add_argument("--skip-asset-url-check", action="store_true", help="Do not fetch generated asset URL during --image-job.")
    parser.add_argument("--strict-launch", action="store_true", help="Require image-job + public asset evidence for final launch sign-off.")
    parser.add_argument("--payment-webhook-replay", action="store_true", help="Replay signed paid/refund payment webhook events against a disposable order.")
    parser.add_argument("--payment-webhook-provider", default="stripe", help="Webhook replay provider: stripe, alipay, wechatpay or generic.")
    parser.add_argument("--payment-webhook-secret", default="", help="Webhook replay HMAC secret matching the deployed provider secret.")
    parser.add_argument("--payment-webhook-order-id", default="", help="Disposable pending order id used for signed webhook replay.")
    parser.add_argument("--payment-webhook-amount-cents", type=int, default=1990, help="Webhook replay order amount in cents.")
    parser.add_argument("--payment-webhook-currency", default="CNY", help="Webhook replay currency.")
    parser.add_argument("--payment-webhook-skip-refund", action="store_true", help="Only replay the paid webhook, not the refund webhook.")
    parser.add_argument("--checkout-initiation", action="store_true", help="Create disposable package/user/order and verify checkout session/link creation.")
    parser.add_argument("--checkout-provider", default="", help="Checkout provider override: manual, redirect or stripe. Empty uses deployed default.")
    parser.add_argument("--checkout-amount-cents", type=int, default=1990, help="Disposable checkout order amount in cents.")
    parser.add_argument("--checkout-currency", default="CNY", help="Disposable checkout order currency.")
    parser.add_argument("--checkout-quota", type=int, default=1, help="Disposable checkout package quota.")
    parser.add_argument("--checkout-webhook-replay", action="store_true", help="After checkout initiation, replay signed paid/refund webhooks against the same disposable checkout order.")
    parser.add_argument("--checkout-webhook-provider", default="", help="Checkout order webhook replay provider. Empty reuses --payment-webhook-provider.")
    parser.add_argument("--checkout-webhook-secret", default="", help="Checkout order webhook replay HMAC secret. Empty reuses --payment-webhook-secret.")
    parser.add_argument("--checkout-webhook-skip-refund", action="store_true", help="Only replay paid webhook for the checkout order, not refund.")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Print machine-readable JSON output.")
    parser.add_argument("--output", help="Optional path to write the JSON evidence report.")
    parser.add_argument("--upload-evidence", action="store_true", help="Upload the JSON report to /api/admin/launch-evidence after verification.")
    parser.add_argument("--evidence-name", default="", help="Evidence name used with --upload-evidence.")
    parser.add_argument("--evidence-source", default="remote-verifier", help="Evidence source used with --upload-evidence.")
    args = parser.parse_args()

    result = verify_deployment(
        base_url=args.base_url,
        admin_key=args.admin_key,
        timeout=args.timeout,
        allow_http=args.allow_http,
        run_image_job=args.image_job,
        prompt=args.prompt,
        model=args.model,
        poll_seconds=args.poll_seconds,
        poll_interval=args.poll_interval,
        check_asset_url=not args.skip_asset_url_check,
        strict_launch=args.strict_launch,
        run_payment_webhook_replay=args.payment_webhook_replay,
        payment_webhook_provider=args.payment_webhook_provider,
        payment_webhook_secret=args.payment_webhook_secret,
        payment_webhook_order_id=args.payment_webhook_order_id,
        payment_webhook_amount_cents=args.payment_webhook_amount_cents,
        payment_webhook_currency=args.payment_webhook_currency,
        payment_webhook_refund=not args.payment_webhook_skip_refund,
        run_checkout_initiation=args.checkout_initiation,
        checkout_provider=args.checkout_provider,
        checkout_amount_cents=args.checkout_amount_cents,
        checkout_currency=args.checkout_currency,
        checkout_quota=args.checkout_quota,
        run_checkout_webhook_replay=args.checkout_webhook_replay,
        checkout_webhook_provider=args.checkout_webhook_provider or args.payment_webhook_provider,
        checkout_webhook_secret=args.checkout_webhook_secret or args.payment_webhook_secret,
        checkout_webhook_refund=not args.checkout_webhook_skip_refund,
    )
    upload_result: dict[str, Any] | None = None
    if args.upload_evidence:
        upload_result = upload_launch_evidence(
            base_url=args.base_url,
            admin_key=args.admin_key,
            report=result,
            name=args.evidence_name,
            source=args.evidence_source,
            timeout=args.timeout,
        )
        result["launch_evidence_upload"] = upload_result
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_human(result)
    upload_ok = True if upload_result is None else bool(upload_result.get("ok"))
    return 0 if result.get("ready") and upload_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
