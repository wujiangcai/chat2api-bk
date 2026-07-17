from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from services.config import DATA_DIR

LOG_TYPE_CALL = "call"
LOG_TYPE_ACCOUNT = "account"

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
    "b64_json",
    "image",
    "input_image",
}
SENSITIVE_KEY_PARTS = (
    "password",
    "secret",
    "token",
    "authorization",
    "cookie",
    "access_token",
    "refresh_token",
    "raw_key",
)
SAFE_EXACT_KEYS = {
    "key_id",
    "provider_payment_id",
    "idempotency_key",
    "code_prefix",
}
MAX_STRING_LENGTH = 2048
REDACTED = "[REDACTED]"
TRUNCATED_SUFFIX = "...[TRUNCATED]"
DATA_IMAGE_URL_RE = re.compile(r"^data:image/([a-z0-9.+-]+);base64,", re.IGNORECASE)


def _is_sensitive_key(key: object) -> bool:
    normalized = str(key or "").strip().lower()
    if not normalized or normalized in SAFE_EXACT_KEYS:
        return False
    if normalized in SENSITIVE_EXACT_KEYS:
        return True
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)


def _truncate(value: str) -> str:
    return value if len(value) <= MAX_STRING_LENGTH else f"{value[:MAX_STRING_LENGTH]}{TRUNCATED_SUFFIX}"


def _sanitize_data_image(value: str) -> str | None:
    match = DATA_IMAGE_URL_RE.match(value.strip())
    if not match:
        return None
    return f"data:image/{match.group(1).lower()};base64,{REDACTED}"


def _sanitize_url_query(value: str) -> str:
    if "?" not in value and "#" not in value:
        return value
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    if not parts.query:
        return value
    query_items = parse_qsl(parts.query, keep_blank_values=True)
    sanitized_items = [
        (key, REDACTED if _is_sensitive_key(key) else item_value)
        for key, item_value in query_items
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(sanitized_items, doseq=True), parts.fragment))


def _sanitize_string(value: str) -> str:
    data_image = _sanitize_data_image(value)
    if data_image is not None:
        return data_image
    return _truncate(_sanitize_url_query(value))


def sanitize_log_value(value: Any, *, key: str = "") -> Any:
    """Recursively redact sensitive fields before persisting or returning logs."""

    if _is_sensitive_key(key):
        return REDACTED
    if isinstance(value, dict):
        return {str(item_key): sanitize_log_value(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [sanitize_log_value(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_log_value(item) for item in value]
    if isinstance(value, set):
        return [sanitize_log_value(item) for item in sorted(value, key=str)]
    if isinstance(value, bytes):
        return f"[BYTES:{len(value)}]"
    if isinstance(value, str):
        return _sanitize_string(value)
    return value


class LogService:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def add(self, type: str, summary: str = "", detail: dict[str, Any] | None = None, **data: Any) -> None:
        raw_detail = detail if detail is not None else data
        item = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "type": type,
            "summary": sanitize_log_value(summary, key="summary"),
            "detail": sanitize_log_value(raw_detail),
        }
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(item, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")

    def list(self, type: str = "", start_date: str = "", end_date: str = "", limit: int = 200) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        items: list[dict[str, Any]] = []
        for line in reversed(self.path.read_text(encoding="utf-8").splitlines()):
            try:
                item = json.loads(line)
            except Exception:
                continue
            t = str(item.get("time") or "")
            day = t[:10]
            if type and item.get("type") != type:
                continue
            if start_date and day < start_date:
                continue
            if end_date and day > end_date:
                continue
            items.append(sanitize_log_value(item))
            if len(items) >= limit:
                break
        return items


log_service = LogService(DATA_DIR / "logs.jsonl")
