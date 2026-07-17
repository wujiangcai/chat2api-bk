from __future__ import annotations

import uuid
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from starlette.requests import Request

from services.config import config
from services.storage.base import StorageBackend

SENSITIVE_EXACT_KEYS = {
    "authorization",
    "cookie",
    "password",
    "token",
    "access_token",
    "refresh_token",
    "secret",
    "secret_key",
    "api_key",
    "raw_key",
    "key",
    "code",
}
SENSITIVE_KEY_PARTS = ("password", "secret", "token", "authorization", "cookie", "access_token", "refresh_token", "raw_key")
MAX_STRING_LENGTH = 2048


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(value: object) -> str:
    return str(value or "").strip()


def _is_sensitive_key(key: object) -> bool:
    normalized = str(key or "").strip().lower()
    if normalized in SENSITIVE_EXACT_KEYS:
        return True
    if normalized in {"key_id", "package_id", "provider_payment_id", "idempotency_key", "code_prefix"}:
        return False
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)


def sanitize_audit_value(value: Any, *, key: str = "") -> Any:
    """Recursively redact sensitive fields before persisting audit details."""

    if _is_sensitive_key(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item_key): sanitize_audit_value(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [sanitize_audit_value(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_audit_value(item) for item in value]
    if isinstance(value, str):
        return value if len(value) <= MAX_STRING_LENGTH else f"{value[:MAX_STRING_LENGTH]}...[TRUNCATED]"
    return value


def _actor_from_identity(identity: dict[str, object] | None) -> dict[str, object]:
    if not isinstance(identity, dict):
        return {}
    actor_id = _clean(identity.get("user_id") or identity.get("id") or identity.get("key_id"))
    key_id = _clean(identity.get("key_id") or identity.get("id"))
    actor = {
        "type": _clean(identity.get("role")) or "unknown",
        "id": actor_id or key_id or None,
        "key_id": key_id or None,
        "email": _clean(identity.get("email")) or None,
        "name": _clean(identity.get("name")) or None,
    }
    return {key: value for key, value in actor.items() if value not in {None, ""}}


def _request_context(request: Request | None) -> dict[str, object]:
    if request is None:
        return {}
    forwarded_for = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    ip = forwarded_for or (request.client.host if request.client else "")
    context = {
        "ip": ip or None,
        "method": request.method,
        "path": request.url.path,
        "user_agent": request.headers.get("user-agent", "") or None,
        "request_id": request.headers.get("x-request-id", "") or None,
    }
    return {key: value for key, value in context.items() if value not in {None, ""}}


class AuditService:
    """Persistent audit trail for admin/security operations."""

    def __init__(self, storage: StorageBackend):
        self.storage = storage
        self._lock = Lock()

    def record(
        self,
        action: str,
        *,
        actor: dict[str, object] | None = None,
        target_type: str = "",
        target_id: str = "",
        status: str = "succeeded",
        summary: str = "",
        detail: dict[str, object] | None = None,
        request: Request | None = None,
    ) -> dict[str, object]:
        normalized_action = _clean(action) or "unknown"
        now = _now_iso()
        target = {
            "type": _clean(target_type) or None,
            "id": _clean(target_id) or None,
        }
        item: dict[str, object] = {
            "id": f"aud_{uuid.uuid4().hex[:16]}",
            "action": normalized_action,
            "status": _clean(status) or "succeeded",
            "summary": _clean(summary),
            "actor": _actor_from_identity(actor),
            "target": {key: value for key, value in target.items() if value not in {None, ""}},
            "request": _request_context(request),
            "detail": sanitize_audit_value(detail or {}),
            "created_at": now,
        }
        with self._lock:
            self.storage.append_collection_item("audit_logs", item)
        return item

    def list_logs(
        self,
        *,
        action: str | None = None,
        actor_id: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        start_date: str = "",
        end_date: str = "",
        limit: int = 200,
    ) -> list[dict[str, object]]:
        try:
            items = self.storage.load_collection("audit_logs")
        except Exception:
            return []
        action_filter = _clean(action)
        actor_filter = _clean(actor_id)
        target_type_filter = _clean(target_type)
        target_id_filter = _clean(target_id)
        start = _clean(start_date)
        end = _clean(end_date)
        rows: list[dict[str, object]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            created_at = _clean(item.get("created_at"))
            day = created_at[:10]
            actor = item.get("actor") if isinstance(item.get("actor"), dict) else {}
            target = item.get("target") if isinstance(item.get("target"), dict) else {}
            if action_filter and _clean(item.get("action")) != action_filter:
                continue
            if actor_filter and _clean(actor.get("id")) != actor_filter and _clean(actor.get("key_id")) != actor_filter:
                continue
            if target_type_filter and _clean(target.get("type")) != target_type_filter:
                continue
            if target_id_filter and _clean(target.get("id")) != target_id_filter:
                continue
            if start and day < start:
                continue
            if end and day > end:
                continue
            rows.append(item)
        rows.sort(key=lambda row: _clean(row.get("created_at")), reverse=True)
        return rows[: max(1, min(int(limit or 200), 1000))]


audit_service = AuditService(config.get_storage_backend())
