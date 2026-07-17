from __future__ import annotations

import os
import shutil
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from services.config import DATA_DIR, config
from services.storage.base import StorageBackend


def _clean(value: object) -> str:
    return str(value or "").strip()


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    return _safe_int(os.getenv(name), default)


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


def _now() -> datetime:
    return datetime.now(UTC)


def _counter_by(items: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts = Counter(_clean(item.get(key)) or "unknown" for item in items if isinstance(item, dict))
    return dict(sorted(counts.items()))


class MonitoringService:
    """Readiness, metrics and alert evaluation for production operations."""

    def __init__(
        self,
        storage_factory: Callable[[], StorageBackend],
        object_storage_factory: Callable[[], Any] | None = None,
        image_job_service_factory: Callable[[], Any] | None = None,
        data_dir: Path = DATA_DIR,
    ):
        self.storage_factory = storage_factory
        self.object_storage_factory = object_storage_factory
        self.image_job_service_factory = image_job_service_factory
        self.data_dir = data_dir

    def live(self, *, version: str = "") -> dict[str, Any]:
        return {"status": "ok", "version": version, "time": _now().isoformat()}

    def collect(self) -> dict[str, Any]:
        started_at = _now()
        checks = self._component_checks()
        metrics = self._business_metrics()
        metrics.update(self._runtime_metrics())
        alerts = self.evaluate_alerts(metrics=metrics, checks=checks)
        status = self._overall_status(checks, alerts)
        return {
            "status": status,
            "time": started_at.isoformat(),
            "duration_ms": int((_now() - started_at).total_seconds() * 1000),
            "checks": checks,
            "metrics": metrics,
            "alerts": alerts,
        }

    def readiness(self) -> dict[str, Any]:
        snapshot = self.collect()
        critical_alerts = [item for item in snapshot["alerts"] if item.get("severity") == "critical"]
        return {
            "status": "unhealthy" if critical_alerts or snapshot["checks"].get("storage", {}).get("status") != "healthy" else snapshot["status"],
            "time": snapshot["time"],
            "checks": snapshot["checks"],
            "alerts": snapshot["alerts"],
        }

    def _component_checks(self) -> dict[str, Any]:
        checks: dict[str, Any] = {}
        try:
            storage = self.storage_factory()
            checks["storage"] = storage.health_check()
        except Exception as exc:
            checks["storage"] = {"status": "unhealthy", "error": str(exc)}

        if self.object_storage_factory is not None:
            try:
                object_storage = self.object_storage_factory()
                checks["object_storage"] = {"status": "healthy", **dict(object_storage.info())}
            except Exception as exc:
                checks["object_storage"] = {"status": "unhealthy", "error": str(exc)}

        if self.image_job_service_factory is not None:
            try:
                service = self.image_job_service_factory()
                checks["image_job_queue"] = {"status": "healthy", **dict(service.queue_info())}
            except Exception as exc:
                checks["image_job_queue"] = {"status": "unhealthy", "error": str(exc)}
        return checks

    def _load_collection(self, name: str) -> list[dict[str, Any]]:
        try:
            storage = self.storage_factory()
            items = storage.load_collection(name)
        except Exception:
            return []
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    def _business_metrics(self) -> dict[str, Any]:
        metrics: dict[str, Any] = {}
        try:
            auth_keys = self.storage_factory().load_auth_keys()
        except Exception:
            auth_keys = []
        metrics["auth_keys_total"] = len(auth_keys) if isinstance(auth_keys, list) else 0

        collections = {
            "users": self._load_collection("users"),
            "orders": self._load_collection("orders"),
            "payments": self._load_collection("payments"),
            "image_jobs": self._load_collection("image_jobs"),
            "image_assets": self._load_collection("image_assets"),
            "quota_ledger": self._load_collection("quota_ledger"),
            "audit_logs": self._load_collection("audit_logs"),
            "support_tickets": self._load_collection("support_tickets"),
        }
        metrics["users_total"] = len(collections["users"])
        metrics["users_enabled_total"] = sum(1 for item in collections["users"] if bool(item.get("enabled", True)))
        metrics["orders_total"] = len(collections["orders"])
        for status, count in _counter_by(collections["orders"], "status").items():
            metrics[f"orders_status_{status}_total"] = count
        metrics["payments_total"] = len(collections["payments"])
        for status, count in _counter_by(collections["payments"], "status").items():
            metrics[f"payments_status_{status}_total"] = count
        metrics["image_jobs_total"] = len(collections["image_jobs"])
        job_status_counts = _counter_by(collections["image_jobs"], "status")
        for status, count in job_status_counts.items():
            metrics[f"image_jobs_status_{status}_total"] = count
        metrics["image_jobs_dead_letter_total"] = sum(
            1
            for item in collections["image_jobs"]
            if item.get("dead_lettered_at") or (isinstance(item.get("error"), dict) and bool((item.get("error") or {}).get("dead_lettered")))
        )
        metrics["image_jobs_stale_running_total"] = self._count_stale_running_jobs(collections["image_jobs"])
        metrics["image_assets_total"] = len(collections["image_assets"])
        for status, count in _counter_by(collections["image_assets"], "status").items():
            metrics[f"image_assets_status_{status}_total"] = count
        metrics["quota_ledger_total"] = len(collections["quota_ledger"])
        metrics["audit_logs_total"] = len(collections["audit_logs"])
        metrics["support_tickets_total"] = len(collections["support_tickets"])
        metrics["support_tickets_attachments_total"] = sum(
            len(message.get("attachments") if isinstance(message.get("attachments"), list) else [])
            for ticket in collections["support_tickets"]
            for message in (ticket.get("messages") or [])
            if isinstance(message, dict)
        )
        for status, count in _counter_by(collections["support_tickets"], "status").items():
            metrics[f"support_tickets_status_{status}_total"] = count
        for priority, count in _counter_by(collections["support_tickets"], "priority").items():
            metrics[f"support_tickets_priority_{priority}_total"] = count
        response_overdue, resolution_overdue = self._count_support_ticket_overdue(collections["support_tickets"])
        metrics["support_tickets_response_overdue_total"] = response_overdue
        metrics["support_tickets_resolution_overdue_total"] = resolution_overdue
        return metrics

    def _runtime_metrics(self) -> dict[str, Any]:
        metrics: dict[str, Any] = {}
        try:
            usage = shutil.disk_usage(self.data_dir)
            metrics["data_disk_total_bytes"] = int(usage.total)
            metrics["data_disk_used_bytes"] = int(usage.used)
            metrics["data_disk_free_bytes"] = int(usage.free)
        except Exception:
            metrics["data_disk_free_bytes"] = -1

        backup_dir = Path(os.getenv("BACKUP_OUTPUT_DIR") or self.data_dir / "backups")
        latest_backup = self._latest_backup_file(backup_dir)
        if latest_backup is None:
            metrics["backup_latest_age_seconds"] = -1
            metrics["backup_latest_size_bytes"] = 0
        else:
            metrics["backup_latest_age_seconds"] = max(0, int(_now().timestamp() - latest_backup.stat().st_mtime))
            metrics["backup_latest_size_bytes"] = int(latest_backup.stat().st_size)
        return metrics

    def _count_stale_running_jobs(self, jobs: list[dict[str, Any]]) -> int:
        threshold = max(1, _env_int("ALERT_RUNNING_JOB_STALE_SECONDS", _env_int("IMAGE_JOB_STALE_RUNNING_SECONDS", 900)))
        cutoff = _now().timestamp() - threshold
        count = 0
        for job in jobs:
            if job.get("status") != "running":
                continue
            started = _parse_datetime(job.get("started_at") or job.get("updated_at"))
            if started is None or started.timestamp() < cutoff:
                count += 1
        return count

    def _count_support_ticket_overdue(self, tickets: list[dict[str, Any]]) -> tuple[int, int]:
        now = _now()
        response_overdue = 0
        resolution_overdue = 0
        for ticket in tickets:
            if ticket.get("status") in {"resolved", "closed"}:
                continue
            first_response_at = _parse_datetime(ticket.get("first_response_at"))
            first_response_due_at = _parse_datetime(ticket.get("first_response_due_at"))
            if first_response_at is None and first_response_due_at is not None and now > first_response_due_at:
                response_overdue += 1
            resolved_at = _parse_datetime(ticket.get("resolved_at"))
            resolution_due_at = _parse_datetime(ticket.get("resolution_due_at"))
            if resolved_at is None and resolution_due_at is not None and now > resolution_due_at:
                resolution_overdue += 1
        return response_overdue, resolution_overdue

    @staticmethod
    def _latest_backup_file(backup_dir: Path) -> Path | None:
        try:
            files = [path for path in backup_dir.glob("chatgpt2api-backup-*.zip") if path.is_file()]
        except Exception:
            return None
        if not files:
            return None
        return max(files, key=lambda path: path.stat().st_mtime)

    def evaluate_alerts(self, *, metrics: dict[str, Any], checks: dict[str, Any]) -> list[dict[str, Any]]:
        alerts: list[dict[str, Any]] = []
        for name, check in checks.items():
            if isinstance(check, dict) and check.get("status") not in {"healthy", "ok"}:
                alerts.append({
                    "code": f"{name}_unhealthy",
                    "severity": "critical" if name == "storage" else "warning",
                    "message": f"{name} check is {check.get('status') or 'unhealthy'}",
                    "value": check.get("status"),
                })

        backlog_threshold = _env_int("ALERT_JOB_QUEUE_BACKLOG_THRESHOLD", 100)
        queued = _safe_int(metrics.get("image_jobs_status_queued_total"), 0)
        if backlog_threshold > 0 and queued >= backlog_threshold:
            alerts.append({"code": "image_job_queue_backlog", "severity": "warning", "message": "queued image jobs exceed threshold", "value": queued, "threshold": backlog_threshold})

        dead_letter_threshold = _env_int("ALERT_DEAD_LETTER_THRESHOLD", 1)
        dead_letter = _safe_int(metrics.get("image_jobs_dead_letter_total"), 0)
        if dead_letter_threshold > 0 and dead_letter >= dead_letter_threshold:
            alerts.append({"code": "image_job_dead_letter", "severity": "warning", "message": "dead-letter image jobs require operator action", "value": dead_letter, "threshold": dead_letter_threshold})

        stale_running = _safe_int(metrics.get("image_jobs_stale_running_total"), 0)
        if stale_running > 0:
            alerts.append({"code": "image_job_stale_running", "severity": "warning", "message": "running image jobs appear stale", "value": stale_running})

        disk_free_mb_threshold = _env_int("ALERT_DISK_FREE_MB", 512)
        free_bytes = _safe_int(metrics.get("data_disk_free_bytes"), -1)
        if disk_free_mb_threshold > 0 and 0 <= free_bytes < disk_free_mb_threshold * 1024 * 1024:
            alerts.append({"code": "data_disk_low", "severity": "critical", "message": "data disk free space is below threshold", "value": free_bytes, "threshold": disk_free_mb_threshold * 1024 * 1024})

        backup_max_age_hours = _env_int("ALERT_BACKUP_MAX_AGE_HOURS", 0)
        backup_age = _safe_int(metrics.get("backup_latest_age_seconds"), -1)
        if backup_max_age_hours > 0 and (backup_age < 0 or backup_age > backup_max_age_hours * 3600):
            alerts.append({"code": "backup_stale", "severity": "warning", "message": "latest backup is missing or too old", "value": backup_age, "threshold": backup_max_age_hours * 3600})
        support_response_threshold = _env_int("ALERT_SUPPORT_RESPONSE_OVERDUE_THRESHOLD", 1)
        support_response_overdue = _safe_int(metrics.get("support_tickets_response_overdue_total"), 0)
        if support_response_threshold > 0 and support_response_overdue >= support_response_threshold:
            alerts.append({
                "code": "support_ticket_response_overdue",
                "severity": "warning",
                "message": "support tickets missed first-response SLA",
                "value": support_response_overdue,
                "threshold": support_response_threshold,
            })
        support_resolution_threshold = _env_int("ALERT_SUPPORT_RESOLUTION_OVERDUE_THRESHOLD", 1)
        support_resolution_overdue = _safe_int(metrics.get("support_tickets_resolution_overdue_total"), 0)
        if support_resolution_threshold > 0 and support_resolution_overdue >= support_resolution_threshold:
            alerts.append({
                "code": "support_ticket_resolution_overdue",
                "severity": "warning",
                "message": "support tickets missed resolution SLA",
                "value": support_resolution_overdue,
                "threshold": support_resolution_threshold,
            })
        return alerts

    @staticmethod
    def _overall_status(checks: dict[str, Any], alerts: list[dict[str, Any]]) -> str:
        if any(item.get("severity") == "critical" for item in alerts):
            return "unhealthy"
        if any(isinstance(check, dict) and check.get("status") not in {"healthy", "ok"} for check in checks.values()):
            return "degraded"
        if alerts:
            return "degraded"
        return "healthy"

    def prometheus_text(self, snapshot: dict[str, Any] | None = None) -> str:
        data = snapshot or self.collect()
        metrics = data.get("metrics") if isinstance(data, dict) else {}
        lines = ["# HELP chatgpt2api_up Service readiness status", "# TYPE chatgpt2api_up gauge"]
        lines.append(f"chatgpt2api_up {1 if data.get('status') != 'unhealthy' else 0}")
        for key, value in sorted((metrics or {}).items()):
            if isinstance(value, (int, float)):
                safe_key = "".join(ch if ch.isalnum() else "_" for ch in key).strip("_")
                lines.append(f"chatgpt2api_{safe_key} {value}")
        lines.append(f"chatgpt2api_alerts_total {len(data.get('alerts') or [])}")
        return "\n".join(lines) + "\n"


def _image_job_service():
    from services.image_job_service import image_job_service

    return image_job_service


monitoring_service = MonitoringService(
    storage_factory=config.get_storage_backend,
    object_storage_factory=config.get_object_storage_backend,
    image_job_service_factory=_image_job_service,
    data_dir=DATA_DIR,
)
