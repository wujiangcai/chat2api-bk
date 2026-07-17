from __future__ import annotations

import os
import re
import uuid
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any

from services.config import config
from services.log_service import sanitize_log_value
from services.object_storage import ObjectStorage
from services.storage.base import StorageBackend


SUPPORT_TICKET_COLLECTION = "support_tickets"
TICKET_STATUSES = {"open", "in_progress", "resolved", "closed"}
TICKET_PRIORITIES = {"low", "normal", "high", "urgent"}
TICKET_CATEGORIES = {"billing", "image", "account", "api", "refund", "other"}
DEFAULT_RESPONSE_SLA_HOURS = {"urgent": 4, "high": 8, "normal": 24, "low": 72}
DEFAULT_RESOLUTION_SLA_HOURS = {"urgent": 24, "high": 48, "normal": 96, "low": 168}
DEFAULT_ATTACHMENT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_ATTACHMENT_ALLOWED_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "application/pdf",
    "text/plain",
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _clean(value: object, *, max_length: int | None = None) -> str:
    text = str(value or "").strip()
    if max_length is not None and len(text) > max_length:
        return text[:max_length]
    return text


def _safe_list(value: object) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _safe_dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_datetime(value: object) -> datetime | None:
    raw = _clean(value)
    if not raw:
        return None
    try:
        candidate = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if candidate.tzinfo is None:
        candidate = candidate.replace(tzinfo=UTC)
    return candidate


def _add_hours(value: str, hours: int) -> str | None:
    base = _parse_datetime(value)
    if base is None or hours <= 0:
        return None
    return (base + timedelta(hours=hours)).isoformat()


def _normalize_status(value: object, default: str = "open") -> str:
    status = _clean(value).lower().replace("-", "_")
    return status if status in TICKET_STATUSES else default


def _normalize_priority(value: object, default: str = "normal") -> str:
    priority = _clean(value).lower().replace("-", "_")
    return priority if priority in TICKET_PRIORITIES else default


def _normalize_category(value: object) -> str:
    category = _clean(value, max_length=64).lower().replace("-", "_")
    return category if category in TICKET_CATEGORIES else "other"


def _normalize_tags(value: object) -> list[str]:
    tags: list[str] = []
    for item in _safe_list(value):
        tag = _clean(item, max_length=32).lower()
        if tag and tag not in tags:
            tags.append(tag)
    return tags[:12]


def _sla_hours(kind: str, priority: str) -> int:
    prefix = "SUPPORT_TICKET_RESPONSE_SLA_HOURS" if kind == "response" else "SUPPORT_TICKET_RESOLUTION_SLA_HOURS"
    normalized_priority = _normalize_priority(priority)
    specific = os.getenv(f"{prefix}_{normalized_priority.upper()}")
    if specific is not None:
        return max(0, _safe_int(specific, 0))
    default = os.getenv(prefix)
    if default is not None:
        return max(0, _safe_int(default, 0))
    defaults = DEFAULT_RESPONSE_SLA_HOURS if kind == "response" else DEFAULT_RESOLUTION_SLA_HOURS
    return defaults.get(normalized_priority, defaults["normal"])


def _attachment_max_bytes() -> int:
    return max(1, _safe_int(os.getenv("SUPPORT_TICKET_ATTACHMENT_MAX_BYTES"), DEFAULT_ATTACHMENT_MAX_BYTES))


def _attachment_allowed_types() -> set[str]:
    raw = _clean(os.getenv("SUPPORT_TICKET_ATTACHMENT_ALLOWED_TYPES"))
    if not raw:
        return set(DEFAULT_ATTACHMENT_ALLOWED_TYPES)
    values = {_clean(item).lower() for item in raw.split(",") if _clean(item)}
    return values or set(DEFAULT_ATTACHMENT_ALLOWED_TYPES)


def _normalize_content_type(content_type: object, filename: object = "") -> str:
    normalized = _clean(content_type, max_length=128).lower()
    if normalized:
        return normalized
    suffix = _clean(filename).lower().rsplit(".", 1)
    if len(suffix) == 2:
        extension = suffix[-1]
        if extension in {"jpg", "jpeg"}:
            return "image/jpeg"
        if extension == "png":
            return "image/png"
        if extension == "webp":
            return "image/webp"
        if extension == "pdf":
            return "application/pdf"
        if extension in {"txt", "log"}:
            return "text/plain"
    return "application/octet-stream"


def _safe_filename(filename: object) -> str:
    value = _clean(filename, max_length=160).replace("\\", "/").split("/")[-1]
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    if not value:
        value = "attachment"
    if len(value) > 120:
        stem, dot, suffix = value.rpartition(".")
        if dot and suffix:
            value = f"{stem[: max(1, 119 - len(suffix))]}.{suffix[:32]}"
        else:
            value = value[:120]
    return value


class SupportTicketService:
    """Commercial customer-support ticket workflow.

    Tickets are stored through the common storage abstraction so JSON, SQLite
    and PostgreSQL deployments share the same API surface. PostgreSQL/SQLite
    backends map the collection to a dedicated queryable table.
    """

    def __init__(self, storage: StorageBackend, object_storage: ObjectStorage | None = None):
        self.storage = storage
        self.object_storage = object_storage
        self._lock = Lock()

    @staticmethod
    def new_ticket_id() -> str:
        return f"tic_{uuid.uuid4().hex[:16]}"

    @staticmethod
    def new_message_id() -> str:
        return f"msg_{uuid.uuid4().hex[:16]}"

    @staticmethod
    def new_attachment_id() -> str:
        return f"att_{uuid.uuid4().hex[:16]}"

    def _object_storage_backend(self) -> ObjectStorage:
        return self.object_storage or config.get_object_storage_backend()

    @staticmethod
    def _identity_id(identity: dict[str, object] | None) -> str:
        return _clean((identity or {}).get("user_id") or (identity or {}).get("id") or (identity or {}).get("key_id"))

    @staticmethod
    def _identity_email(identity: dict[str, object] | None) -> str:
        return _clean((identity or {}).get("email"), max_length=320)

    @staticmethod
    def _identity_name(identity: dict[str, object] | None) -> str:
        return _clean((identity or {}).get("name"), max_length=128)

    @staticmethod
    def _is_admin(identity: dict[str, object] | None) -> bool:
        return (identity or {}).get("role") == "admin"

    @staticmethod
    def _message(
        *,
        author_type: str,
        author_id: str,
        author_email: str = "",
        author_name: str = "",
        body: str,
        internal: bool = False,
        created_at: str | None = None,
        message_id: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        text = _clean(body, max_length=5000)
        safe_attachments = [dict(item) for item in (attachments or []) if isinstance(item, dict)]
        if not text and not safe_attachments:
            raise ValueError("message is required")
        return {
            "id": _clean(message_id, max_length=255) or SupportTicketService.new_message_id(),
            "author_type": author_type if author_type in {"user", "admin", "system"} else "user",
            "author_id": _clean(author_id, max_length=255),
            "author_email": _clean(author_email, max_length=320) or None,
            "author_name": _clean(author_name, max_length=128) or None,
            "body": text,
            "internal": bool(internal),
            "created_at": created_at or _now_iso(),
            "attachments": safe_attachments,
        }

    def _normalize_attachment(self, raw: object) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        attachment_id = _clean(raw.get("id"), max_length=255) or self.new_attachment_id()
        filename = _safe_filename(raw.get("filename") or "attachment")
        object_key = _clean(raw.get("object_key"), max_length=1024)
        url = _clean(raw.get("url"), max_length=2048)
        if not object_key and not url:
            return None
        content_type = _normalize_content_type(raw.get("content_type"), filename)
        return {
            "id": attachment_id,
            "filename": filename,
            "content_type": content_type,
            "size_bytes": max(0, _safe_int(raw.get("size_bytes"), 0)),
            "object_key": object_key or None,
            "url": url or None,
            "uploaded_by": _clean(raw.get("uploaded_by"), max_length=32) or None,
            "uploader_id": _clean(raw.get("uploader_id"), max_length=255) or None,
            "internal": bool(raw.get("internal")),
            "created_at": _clean(raw.get("created_at")) or _now_iso(),
        }

    @staticmethod
    def _validate_attachment(*, filename: str, content_type: str, data: bytes) -> tuple[str, str]:
        safe_filename = _safe_filename(filename)
        normalized_content_type = _normalize_content_type(content_type, safe_filename)
        if not data:
            raise ValueError("attachment file is required")
        max_bytes = _attachment_max_bytes()
        if len(data) > max_bytes:
            raise ValueError(f"attachment exceeds maximum size ({max_bytes} bytes)")
        if normalized_content_type not in _attachment_allowed_types():
            raise ValueError(f"attachment content type is not allowed: {normalized_content_type}")
        return safe_filename, normalized_content_type

    def _normalize(self, raw: object) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        ticket_id = _clean(raw.get("id"), max_length=255)
        if not ticket_id:
            return None
        now = _now_iso()
        messages = []
        for item in _safe_list(raw.get("messages")):
            if not isinstance(item, dict):
                continue
            body = _clean(item.get("body"), max_length=5000)
            attachments = [attachment for raw_attachment in _safe_list(item.get("attachments")) if (attachment := self._normalize_attachment(raw_attachment)) is not None]
            if not body and not attachments:
                continue
            messages.append(
                {
                    "id": _clean(item.get("id"), max_length=255) or self.new_message_id(),
                    "author_type": _clean(item.get("author_type")) if _clean(item.get("author_type")) in {"user", "admin", "system"} else "user",
                    "author_id": _clean(item.get("author_id"), max_length=255),
                    "author_email": _clean(item.get("author_email"), max_length=320) or None,
                    "author_name": _clean(item.get("author_name"), max_length=128) or None,
                    "body": body,
                    "internal": bool(item.get("internal")),
                    "created_at": _clean(item.get("created_at")) or now,
                    "attachments": attachments,
                }
            )
        created_at = _clean(raw.get("created_at")) or now
        updated_at = _clean(raw.get("updated_at")) or created_at
        last_message_at = _clean(raw.get("last_message_at")) or (messages[-1]["created_at"] if messages else updated_at)
        status = _normalize_status(raw.get("status"))
        priority = _normalize_priority(raw.get("priority"))
        first_response_due_at = _clean(raw.get("first_response_due_at")) or _add_hours(created_at, _sla_hours("response", priority))
        resolution_due_at = _clean(raw.get("resolution_due_at")) or _add_hours(created_at, _sla_hours("resolution", priority))
        return {
            "id": ticket_id,
            "user_id": _clean(raw.get("user_id"), max_length=255),
            "email": _clean(raw.get("email"), max_length=320) or None,
            "name": _clean(raw.get("name"), max_length=128) or None,
            "subject": _clean(raw.get("subject"), max_length=160) or "Untitled support ticket",
            "category": _normalize_category(raw.get("category")),
            "priority": priority,
            "status": status,
            "assignee_id": _clean(raw.get("assignee_id"), max_length=255) or None,
            "assignee_name": _clean(raw.get("assignee_name"), max_length=128) or None,
            "created_at": created_at,
            "updated_at": updated_at,
            "last_message_at": last_message_at,
            "first_response_due_at": first_response_due_at,
            "first_response_at": _clean(raw.get("first_response_at")) or None,
            "resolution_due_at": resolution_due_at,
            "resolved_at": _clean(raw.get("resolved_at")) or None,
            "closed_at": _clean(raw.get("closed_at")) or None,
            "tags": _normalize_tags(raw.get("tags")),
            "metadata": _safe_dict(raw.get("metadata")),
            "notifications": _safe_list(raw.get("notifications")),
            "messages": messages,
        }

    def _load(self) -> list[dict[str, Any]]:
        try:
            raw_items = self.storage.load_collection(SUPPORT_TICKET_COLLECTION)
        except Exception:
            raw_items = []
        items = [item for raw in raw_items if (item := self._normalize(raw)) is not None]
        items.sort(key=lambda item: _clean(item.get("last_message_at") or item.get("updated_at")), reverse=True)
        return items

    def _save_all(self, items: list[dict[str, Any]]) -> None:
        self.storage.save_collection(SUPPORT_TICKET_COLLECTION, items)

    def _save_one(self, item: dict[str, Any]) -> None:
        self.storage.append_collection_item(SUPPORT_TICKET_COLLECTION, item)

    @staticmethod
    def _sla_state(item: dict[str, Any]) -> dict[str, Any]:
        status = _clean(item.get("status"))
        now = datetime.now(UTC)
        first_response_at = _parse_datetime(item.get("first_response_at"))
        first_response_due_at = _parse_datetime(item.get("first_response_due_at"))
        resolved_at = _parse_datetime(item.get("resolved_at"))
        resolution_due_at = _parse_datetime(item.get("resolution_due_at"))
        response_overdue_seconds = 0
        resolution_overdue_seconds = 0
        if status not in {"resolved", "closed"}:
            if first_response_at is None and first_response_due_at is not None and now > first_response_due_at:
                response_overdue_seconds = int((now - first_response_due_at).total_seconds())
            if resolved_at is None and resolution_due_at is not None and now > resolution_due_at:
                resolution_overdue_seconds = int((now - resolution_due_at).total_seconds())
        if response_overdue_seconds > 0:
            sla_status = "response_overdue"
        elif resolution_overdue_seconds > 0:
            sla_status = "resolution_overdue"
        elif status in {"resolved", "closed"}:
            sla_status = "resolved"
        else:
            sla_status = "on_track"
        return {
            "sla_status": sla_status,
            "response_overdue_seconds": response_overdue_seconds,
            "resolution_overdue_seconds": resolution_overdue_seconds,
            "overdue_seconds": max(response_overdue_seconds, resolution_overdue_seconds),
        }

    @staticmethod
    def _public(item: dict[str, Any], *, include_messages: bool = True, include_internal: bool = False) -> dict[str, Any]:
        messages = []
        if include_messages:
            for message in _safe_list(item.get("messages")):
                if not include_internal and isinstance(message, dict) and message.get("internal"):
                    continue
                message_copy = dict(message)
                attachments = []
                for attachment in _safe_list(message_copy.get("attachments")):
                    if not isinstance(attachment, dict):
                        continue
                    if not include_internal and attachment.get("internal"):
                        continue
                    attachments.append(dict(attachment))
                message_copy["attachments"] = attachments
                messages.append(message_copy)
        sla = SupportTicketService._sla_state(item)
        return {
            "id": item.get("id"),
            "user_id": item.get("user_id"),
            "email": item.get("email"),
            "name": item.get("name"),
            "subject": item.get("subject"),
            "category": item.get("category"),
            "priority": item.get("priority"),
            "status": item.get("status"),
            "assignee_id": item.get("assignee_id") if include_internal else None,
            "assignee_name": item.get("assignee_name"),
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
            "last_message_at": item.get("last_message_at"),
            "first_response_due_at": item.get("first_response_due_at"),
            "first_response_at": item.get("first_response_at"),
            "resolution_due_at": item.get("resolution_due_at"),
            "resolved_at": item.get("resolved_at"),
            "closed_at": item.get("closed_at"),
            **sla,
            "tags": list(item.get("tags") or []) if include_internal else [],
            "metadata": dict(item.get("metadata") or {}) if include_internal else {},
            "notifications": list(item.get("notifications") or []) if include_internal else [],
            "message_count": len([m for m in _safe_list(item.get("messages")) if include_internal or not (isinstance(m, dict) and m.get("internal"))]),
            "messages": messages,
        }

    @staticmethod
    def _can_access(item: dict[str, Any], identity: dict[str, object]) -> bool:
        if SupportTicketService._is_admin(identity):
            return True
        user_id = SupportTicketService._identity_id(identity)
        return bool(user_id and user_id == _clean(item.get("user_id")))

    def create_ticket(
        self,
        identity: dict[str, object],
        *,
        subject: str,
        message: str,
        category: str = "other",
        priority: str = "normal",
        metadata: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        user_id = self._identity_id(identity)
        if not user_id or identity.get("role") != "user":
            raise ValueError("registered user is required")
        clean_subject = _clean(subject, max_length=160)
        if not clean_subject:
            raise ValueError("subject is required")
        now = _now_iso()
        first_message = self._message(
            author_type="user",
            author_id=user_id,
            author_email=self._identity_email(identity),
            author_name=self._identity_name(identity),
            body=message,
            created_at=now,
        )
        item = {
            "id": self.new_ticket_id(),
            "user_id": user_id,
            "email": self._identity_email(identity) or None,
            "name": self._identity_name(identity) or None,
            "subject": clean_subject,
            "category": _normalize_category(category),
            "priority": _normalize_priority(priority),
            "status": "open",
            "assignee_id": None,
            "assignee_name": None,
            "created_at": now,
            "updated_at": now,
            "last_message_at": now,
            "first_response_due_at": _add_hours(now, _sla_hours("response", priority)),
            "first_response_at": None,
            "resolution_due_at": _add_hours(now, _sla_hours("resolution", priority)),
            "resolved_at": None,
            "closed_at": None,
            "tags": [],
            "metadata": sanitize_log_value(metadata or {}),
            "notifications": [],
            "messages": [first_message],
        }
        normalized = self._normalize(item)
        if normalized is None:
            raise ValueError("ticket is invalid")
        with self._lock:
            self._save_one(normalized)
        return self._public(normalized)

    def list_tickets(
        self,
        identity: dict[str, object],
        *,
        status: str = "",
        priority: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        safe_limit = min(max(1, int(limit or 100)), 1000)
        include_internal = self._is_admin(identity)
        normalized_status = _clean(status).lower()
        normalized_priority = _clean(priority).lower()
        items = []
        for item in self._load():
            if not self._can_access(item, identity):
                continue
            if normalized_status and item.get("status") != normalized_status:
                continue
            if normalized_priority and item.get("priority") != normalized_priority:
                continue
            items.append(self._public(item, include_messages=False, include_internal=include_internal))
            if len(items) >= safe_limit:
                break
        return items

    def get_ticket(self, ticket_id: str, identity: dict[str, object]) -> dict[str, Any] | None:
        normalized_id = _clean(ticket_id)
        if not normalized_id:
            return None
        include_internal = self._is_admin(identity)
        for item in self._load():
            if item.get("id") != normalized_id:
                continue
            if not self._can_access(item, identity):
                return None
            return self._public(item, include_internal=include_internal)
        return None

    def add_message(
        self,
        ticket_id: str,
        identity: dict[str, object],
        *,
        message: str,
        internal: bool = False,
    ) -> dict[str, Any] | None:
        normalized_id = _clean(ticket_id)
        if not normalized_id:
            return None
        with self._lock:
            items = self._load()
            for index, item in enumerate(items):
                if item.get("id") != normalized_id:
                    continue
                if not self._can_access(item, identity):
                    return None
                if item.get("status") == "closed" and not self._is_admin(identity):
                    raise ValueError("ticket is closed")
                author_type = "admin" if self._is_admin(identity) else "user"
                now = _now_iso()
                next_item = dict(item)
                next_messages = list(next_item.get("messages") or [])
                next_messages.append(
                    self._message(
                        author_type=author_type,
                        author_id=self._identity_id(identity),
                        author_email=self._identity_email(identity),
                        author_name=self._identity_name(identity),
                        body=message,
                        internal=internal and self._is_admin(identity),
                        created_at=now,
                    )
                )
                next_item["messages"] = next_messages
                if item.get("status") == "resolved" and not self._is_admin(identity):
                    next_item["status"] = "open"
                    next_item["resolved_at"] = None
                    next_item["resolution_due_at"] = _add_hours(now, _sla_hours("resolution", str(next_item.get("priority") or "normal")))
                elif self._is_admin(identity) and item.get("status") == "open":
                    next_item["status"] = "in_progress"
                if self._is_admin(identity) and not internal and not next_item.get("first_response_at"):
                    next_item["first_response_at"] = now
                next_item["updated_at"] = now
                next_item["last_message_at"] = now
                items[index] = next_item
                self._save_all(items)
                return self._public(next_item, include_internal=self._is_admin(identity))
        return None

    def add_attachment(
        self,
        ticket_id: str,
        identity: dict[str, object],
        *,
        filename: str,
        content_type: str,
        data: bytes,
        message: str = "",
        internal: bool = False,
        base_url: str = "",
    ) -> dict[str, Any] | None:
        normalized_id = _clean(ticket_id)
        if not normalized_id:
            return None
        safe_filename, normalized_content_type = self._validate_attachment(
            filename=filename,
            content_type=content_type,
            data=data,
        )
        with self._lock:
            items = self._load()
            for index, item in enumerate(items):
                if item.get("id") != normalized_id:
                    continue
                if not self._can_access(item, identity):
                    return None
                if item.get("status") == "closed" and not self._is_admin(identity):
                    raise ValueError("ticket is closed")
                author_type = "admin" if self._is_admin(identity) else "user"
                is_internal = bool(internal and self._is_admin(identity))
                now = _now_iso()
                message_id = self.new_message_id()
                attachment_id = self.new_attachment_id()
                object_key = f"support/{normalized_id}/{message_id}/{attachment_id}-{safe_filename}"
                object_storage = self._object_storage_backend()
                stored_key = ""
                try:
                    stored_key = object_storage.put_object(object_key, data, normalized_content_type)
                    attachment = {
                        "id": attachment_id,
                        "filename": safe_filename,
                        "content_type": normalized_content_type,
                        "size_bytes": len(data),
                        "object_key": stored_key,
                        "url": object_storage.public_url(stored_key, base_url=base_url),
                        "uploaded_by": author_type,
                        "uploader_id": self._identity_id(identity) or None,
                        "internal": is_internal,
                        "created_at": now,
                    }
                    next_item = dict(item)
                    next_messages = list(next_item.get("messages") or [])
                    next_messages.append(
                        self._message(
                            author_type=author_type,
                            author_id=self._identity_id(identity),
                            author_email=self._identity_email(identity),
                            author_name=self._identity_name(identity),
                            body=_clean(message, max_length=5000) or f"附件：{safe_filename}",
                            internal=is_internal,
                            created_at=now,
                            message_id=message_id,
                            attachments=[attachment],
                        )
                    )
                    next_item["messages"] = next_messages
                    if item.get("status") == "resolved" and not self._is_admin(identity):
                        next_item["status"] = "open"
                        next_item["resolved_at"] = None
                        next_item["resolution_due_at"] = _add_hours(now, _sla_hours("resolution", str(next_item.get("priority") or "normal")))
                    elif self._is_admin(identity) and item.get("status") == "open":
                        next_item["status"] = "in_progress"
                    if self._is_admin(identity) and not is_internal and not next_item.get("first_response_at"):
                        next_item["first_response_at"] = now
                    next_item["updated_at"] = now
                    next_item["last_message_at"] = now
                    items[index] = next_item
                    self._save_all(items)
                    return self._public(next_item, include_internal=self._is_admin(identity))
                except Exception:
                    if stored_key:
                        try:
                            object_storage.delete_object(stored_key)
                        except Exception:
                            pass
                    raise
        return None

    def update_ticket(
        self,
        ticket_id: str,
        actor: dict[str, object],
        *,
        status: str | None = None,
        priority: str | None = None,
        assignee_id: str | None = None,
        assignee_name: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, Any] | None:
        if not self._is_admin(actor):
            raise ValueError("admin permission required")
        normalized_id = _clean(ticket_id)
        if not normalized_id:
            return None
        with self._lock:
            items = self._load()
            for index, item in enumerate(items):
                if item.get("id") != normalized_id:
                    continue
                now = _now_iso()
                next_item = dict(item)
                if status is not None:
                    next_status = _normalize_status(status, default=_clean(item.get("status")) or "open")
                    next_item["status"] = next_status
                    if next_status == "resolved" and not next_item.get("resolved_at"):
                        next_item["resolved_at"] = now
                    if next_status == "closed" and not next_item.get("closed_at"):
                        next_item["closed_at"] = now
                    if next_status in {"open", "in_progress"}:
                        next_item["closed_at"] = None
                        if next_status == "open":
                            next_item["resolved_at"] = None
                if priority is not None:
                    next_priority = _normalize_priority(priority, default=_clean(item.get("priority")) or "normal")
                    next_item["priority"] = next_priority
                    if not next_item.get("first_response_at"):
                        next_item["first_response_due_at"] = _add_hours(str(next_item.get("created_at") or now), _sla_hours("response", next_priority))
                    if next_item.get("status") not in {"resolved", "closed"}:
                        next_item["resolution_due_at"] = _add_hours(str(next_item.get("created_at") or now), _sla_hours("resolution", next_priority))
                if assignee_id is not None:
                    next_item["assignee_id"] = _clean(assignee_id, max_length=255) or None
                if assignee_name is not None:
                    next_item["assignee_name"] = _clean(assignee_name, max_length=128) or None
                if tags is not None:
                    next_item["tags"] = _normalize_tags(tags)
                if metadata is not None:
                    sanitized_metadata = sanitize_log_value(metadata)
                    next_item["metadata"] = sanitized_metadata if isinstance(sanitized_metadata, dict) else {}
                next_item["updated_at"] = now
                items[index] = next_item
                self._save_all(items)
                return self._public(next_item, include_internal=True)
        return None

    def record_notification(
        self,
        ticket_id: str,
        *,
        event: str,
        channel: str,
        recipient: str,
        status: str,
        message: str = "",
    ) -> dict[str, Any] | None:
        normalized_id = _clean(ticket_id)
        if not normalized_id:
            return None
        with self._lock:
            items = self._load()
            for index, item in enumerate(items):
                if item.get("id") != normalized_id:
                    continue
                next_item = dict(item)
                notifications = list(next_item.get("notifications") or [])
                notifications.append(
                    {
                        "id": f"ntf_{uuid.uuid4().hex[:16]}",
                        "event": _clean(event, max_length=64) or "ticket_update",
                        "channel": _clean(channel, max_length=32) or "email",
                        "recipient": _clean(recipient, max_length=320) or None,
                        "status": _clean(status, max_length=32) or "unknown",
                        "message": _clean(message, max_length=300) or None,
                        "created_at": _now_iso(),
                    }
                )
                next_item["notifications"] = notifications[-50:]
                next_item["updated_at"] = _now_iso()
                items[index] = next_item
                self._save_all(items)
                return self._public(next_item, include_internal=True)
        return None

    def stats(self) -> dict[str, Any]:
        items = self._load()
        status_counts: dict[str, int] = {}
        priority_counts: dict[str, int] = {}
        response_overdue = 0
        resolution_overdue = 0
        attachments_total = 0
        for item in items:
            status = _clean(item.get("status")) or "unknown"
            priority = _clean(item.get("priority")) or "normal"
            status_counts[status] = status_counts.get(status, 0) + 1
            priority_counts[priority] = priority_counts.get(priority, 0) + 1
            attachments_total += sum(len(_safe_list(message.get("attachments"))) for message in _safe_list(item.get("messages")) if isinstance(message, dict))
            sla = self._sla_state(item)
            if sla.get("response_overdue_seconds"):
                response_overdue += 1
            if sla.get("resolution_overdue_seconds"):
                resolution_overdue += 1
        return {
            "total": len(items),
            "by_status": status_counts,
            "by_priority": priority_counts,
            "attachments_total": attachments_total,
            "response_overdue_total": response_overdue,
            "resolution_overdue_total": resolution_overdue,
        }


support_ticket_service = SupportTicketService(config.get_storage_backend())
