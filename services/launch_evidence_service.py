from __future__ import annotations

import uuid
from datetime import UTC, datetime
from threading import Lock
from typing import Any

from services.config import config
from services.log_service import sanitize_log_value
from services.storage.base import StorageBackend


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _clean(value: object) -> str:
    return str(value or "").strip()


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _list(value: object) -> list[Any]:
    return list(value) if isinstance(value, list) else []


class LaunchEvidenceService:
    """Persist production deployment verification reports for launch sign-off."""

    def __init__(self, storage: StorageBackend):
        self.storage = storage
        self._lock = Lock()

    @staticmethod
    def new_id() -> str:
        return f"lev_{uuid.uuid4().hex[:16]}"

    def _normalize(self, raw: object) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        evidence_id = _clean(raw.get("id"))
        if not evidence_id:
            return None
        report = _dict(raw.get("report"))
        summary = _dict(raw.get("summary") or report.get("summary"))
        checks = _list(raw.get("checks") or raw.get("items") or report.get("checks") or report.get("items"))
        status = _clean(raw.get("status") or report.get("status")) or "unknown"
        created_at = _clean(raw.get("created_at")) or _now_iso()
        return {
            "id": evidence_id,
            "name": _clean(raw.get("name")) or f"launch evidence {created_at[:19]}",
            "status": status,
            "ready": bool(raw.get("ready", report.get("ready", status == "passed"))),
            "source": _clean(raw.get("source")) or "manual-upload",
            "base_url": _clean(raw.get("base_url") or report.get("base_url")) or None,
            "generated_at": _clean(raw.get("generated_at") or report.get("generated_at")) or None,
            "created_at": created_at,
            "created_by": _clean(raw.get("created_by")) or None,
            "summary": {
                "total": _safe_int(summary.get("total"), len(checks)),
                "passed": _safe_int(summary.get("passed")),
                "warning": _safe_int(summary.get("warning")),
                "failed": _safe_int(summary.get("failed")),
            },
            "failed_checks": [
                {
                    "id": _clean(item.get("id")),
                    "message": _clean(item.get("message")),
                }
                for item in checks
                if isinstance(item, dict) and item.get("status") == "failed"
            ][:20],
            "report": report,
        }

    @staticmethod
    def _public(item: dict[str, Any], *, include_report: bool = False) -> dict[str, Any]:
        public = {
            "id": item.get("id"),
            "name": item.get("name"),
            "status": item.get("status"),
            "ready": item.get("ready"),
            "source": item.get("source"),
            "base_url": item.get("base_url"),
            "generated_at": item.get("generated_at"),
            "created_at": item.get("created_at"),
            "created_by": item.get("created_by"),
            "summary": dict(item.get("summary") or {}),
            "failed_checks": list(item.get("failed_checks") or []),
        }
        if include_report:
            public["report"] = item.get("report") or {}
        return public

    def _load(self) -> list[dict[str, Any]]:
        try:
            items = self.storage.load_collection("launch_evidence")
        except Exception:
            return []
        normalized = [item for raw in items if (item := self._normalize(raw)) is not None]
        normalized.sort(key=lambda item: _clean(item.get("created_at")), reverse=True)
        return normalized

    def create(
        self,
        report: dict[str, Any],
        *,
        actor: dict[str, object] | None = None,
        name: str = "",
        source: str = "manual-upload",
    ) -> dict[str, Any]:
        if not isinstance(report, dict) or not report:
            raise ValueError("report is required")
        sanitized_report = sanitize_log_value(report)
        if not isinstance(sanitized_report, dict):
            raise ValueError("report is invalid")
        now = _now_iso()
        created_by = _clean((actor or {}).get("user_id") or (actor or {}).get("id") or (actor or {}).get("key_id"))
        item = self._normalize(
            {
                "id": self.new_id(),
                "name": name,
                "source": source,
                "created_at": now,
                "created_by": created_by,
                "report": sanitized_report,
            }
        )
        if item is None:
            raise ValueError("report is invalid")
        with self._lock:
            self.storage.append_collection_item("launch_evidence", item)
        return self._public(item, include_report=True)

    def list(self, *, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = min(max(1, int(limit or 50)), 500)
        return [self._public(item) for item in self._load()[:safe_limit]]

    def get(self, evidence_id: str) -> dict[str, Any] | None:
        normalized_id = _clean(evidence_id)
        if not normalized_id:
            return None
        for item in self._load():
            if item.get("id") == normalized_id:
                return self._public(item, include_report=True)
        return None

    def delete(self, evidence_id: str) -> bool:
        normalized_id = _clean(evidence_id)
        if not normalized_id:
            return False
        with self._lock:
            items = self._load()
            next_items = [item for item in items if item.get("id") != normalized_id]
            if len(next_items) == len(items):
                return False
            self.storage.save_collection("launch_evidence", next_items)
            return True


launch_evidence_service = LaunchEvidenceService(config.get_storage_backend())
