from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread
from typing import Any, Literal

from services.auth_service import AuthService, auth_service
from services.config import config
from services.image_asset_service import image_asset_service
from services.image_job_queue import ImageJobCoordinator, create_image_job_coordinator_from_env
from services.storage.base import StorageBackend

ImageJobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]

TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(value: object) -> str:
    return str(value or "").strip()


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
        candidate = candidate.replace(tzinfo=timezone.utc)
    return candidate


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _count_success_items(result: object, fallback: int) -> int:
    if not isinstance(result, dict):
        return max(0, int(fallback or 0))
    items = result.get("data")
    if not isinstance(items, list):
        return 0 if result.get("error") else max(0, int(fallback or 0))
    count = sum(
        1
        for item in items
        if isinstance(item, dict) and (item.get("b64_json") or item.get("url")) and not item.get("error")
    )
    return count if count > 0 else 0


class ImageJobService:
    """Durable image job queue MVP.

    The first production step uses the existing storage abstraction so jobs
    survive process restarts. The API is intentionally queue-shaped, making it
    straightforward to replace the polling worker with Redis/RQ/Celery later.
    """

    def __init__(
        self,
        storage: StorageBackend,
        auth: AuthService,
        asset_service: Any | None = None,
        coordinator: ImageJobCoordinator | None = None,
        default_max_attempts: int | None = None,
        retry_delay_seconds: int | None = None,
        stale_running_seconds: int | None = None,
    ):
        self.storage = storage
        self.auth_service = auth
        self.asset_service = asset_service or image_asset_service
        self.coordinator = coordinator
        self.default_max_attempts = max(1, int(default_max_attempts or os.getenv("IMAGE_JOB_MAX_ATTEMPTS", "1") or "1"))
        self.retry_delay_seconds = max(0, int(retry_delay_seconds if retry_delay_seconds is not None else os.getenv("IMAGE_JOB_RETRY_DELAY_SECONDS", "5") or "5"))
        self.stale_running_seconds = max(30, int(stale_running_seconds if stale_running_seconds is not None else os.getenv("IMAGE_JOB_STALE_RUNNING_SECONDS", "900") or "900"))
        self._lock = Lock()
        self._jobs = self._load_jobs()

    @staticmethod
    def new_job_id() -> str:
        return f"job_{uuid.uuid4().hex[:16]}"

    @staticmethod
    def _normalize_request(raw: object) -> dict[str, object]:
        if not isinstance(raw, dict):
            raw = {}
        prompt = _clean(raw.get("prompt"))
        return {
            "prompt": prompt,
            "model": _clean(raw.get("model")) or "gpt-image-2",
            "n": max(1, min(4, _safe_int(raw.get("n"), 1))),
            "size": _clean(raw.get("size")) or None,
            "response_format": _clean(raw.get("response_format")) or "b64_json",
            "max_attempts": max(1, _safe_int(raw.get("max_attempts"), 0)) if raw.get("max_attempts") is not None else None,
        }

    @staticmethod
    def _normalize_owner(raw: object) -> dict[str, object]:
        if not isinstance(raw, dict):
            raw = {}
        return {
            "role": _clean(raw.get("role")) or "user",
            "key_id": _clean(raw.get("key_id") or raw.get("id")) or None,
            "key_name": _clean(raw.get("key_name") or raw.get("name")) or None,
            "user_id": _clean(raw.get("user_id")) or None,
            "email": _clean(raw.get("email")) or None,
        }

    def _normalize_job(self, raw: object) -> dict[str, object] | None:
        if not isinstance(raw, dict):
            return None
        job_id = _clean(raw.get("id"))
        if not job_id:
            return None
        status = _clean(raw.get("status")) or "queued"
        if status not in {"queued", "running", "succeeded", "failed", "cancelled"}:
            status = "queued"
        request = self._normalize_request(raw.get("request"))
        owner = self._normalize_owner(raw.get("owner"))
        created_at = _clean(raw.get("created_at")) or _now_iso()
        updated_at = _clean(raw.get("updated_at")) or created_at
        return {
            "id": job_id,
            "type": _clean(raw.get("type")) or "image.generation",
            "status": status,
            "owner": owner,
            "request": request,
            "prompt_preview": _clean(raw.get("prompt_preview")) or request.get("prompt", "")[:120],
            "base_url": _clean(raw.get("base_url")) or "",
            "reserved_quota": max(0, _safe_int(raw.get("reserved_quota"), 0)),
            "refunded_quota": max(0, _safe_int(raw.get("refunded_quota"), 0)),
            "cost_units": max(0, _safe_int(raw.get("cost_units"), 0)),
            "attempts": max(0, _safe_int(raw.get("attempts"), 0)),
            "max_attempts": max(1, _safe_int(raw.get("max_attempts"), self.default_max_attempts)),
            "next_run_after": _clean(raw.get("next_run_after")) or None,
            "dead_lettered_at": _clean(raw.get("dead_lettered_at")) or None,
            "result": raw.get("result") if isinstance(raw.get("result"), dict) else None,
            "assets": raw.get("assets") if isinstance(raw.get("assets"), list) else [],
            "error": raw.get("error") if isinstance(raw.get("error"), dict) else None,
            "created_at": created_at,
            "updated_at": updated_at,
            "started_at": _clean(raw.get("started_at")) or None,
            "completed_at": _clean(raw.get("completed_at")) or None,
        }

    def _load_jobs(self) -> list[dict[str, object]]:
        try:
            items = self.storage.load_collection("image_jobs")
        except Exception:
            return []
        if not isinstance(items, list):
            return []
        jobs = [normalized for item in items if (normalized := self._normalize_job(item)) is not None]
        jobs.sort(key=lambda item: str(item.get("created_at") or ""))
        return jobs

    def _save_jobs(self) -> None:
        self.storage.save_collection("image_jobs", self._jobs)

    def _save_job(self, job: dict[str, object]) -> None:
        self.storage.append_collection_item("image_jobs", job)

    def _refresh_jobs_from_storage(self) -> None:
        self._jobs = self._load_jobs()

    def queue_info(self) -> dict[str, object]:
        info = {"backend": "storage-polling"} if self.coordinator is None else self.coordinator.info()
        info.update({
            "default_max_attempts": self.default_max_attempts,
            "retry_delay_seconds": self.retry_delay_seconds,
            "stale_running_seconds": self.stale_running_seconds,
        })
        return info

    @staticmethod
    def _public_job(job: dict[str, object], *, include_result: bool = True) -> dict[str, object]:
        public = {
            "id": job.get("id"),
            "type": job.get("type"),
            "status": job.get("status"),
            "owner": dict(job.get("owner") or {}),
            "request": dict(job.get("request") or {}),
            "prompt_preview": job.get("prompt_preview"),
            "reserved_quota": job.get("reserved_quota"),
            "refunded_quota": job.get("refunded_quota"),
            "cost_units": job.get("cost_units"),
            "attempts": job.get("attempts"),
            "max_attempts": job.get("max_attempts"),
            "next_run_after": job.get("next_run_after"),
            "dead_lettered_at": job.get("dead_lettered_at"),
            "assets": list(job.get("assets") or []),
            "error": dict(job.get("error") or {}) if job.get("error") else None,
            "created_at": job.get("created_at"),
            "updated_at": job.get("updated_at"),
            "started_at": job.get("started_at"),
            "completed_at": job.get("completed_at"),
        }
        if include_result:
            public["result"] = job.get("result")
        return public

    @staticmethod
    def _can_access(identity: dict[str, object], job: dict[str, object]) -> bool:
        if identity.get("role") == "admin":
            return True
        owner = job.get("owner") if isinstance(job.get("owner"), dict) else {}
        user_id = _clean(identity.get("user_id"))
        key_id = _clean(identity.get("key_id") or identity.get("id"))
        return bool(
            (user_id and owner.get("user_id") == user_id)
            or (key_id and owner.get("key_id") == key_id)
        )

    def enqueue_generation(
        self,
        *,
        job_id: str,
        identity: dict[str, object],
        request: dict[str, object],
        base_url: str = "",
        reserved_quota: int = 0,
    ) -> dict[str, object]:
        normalized_request = self._normalize_request(request)
        if not normalized_request.get("prompt"):
            raise ValueError("prompt is required")
        now = _now_iso()
        job = {
            "id": job_id,
            "type": "image.generation",
            "status": "queued",
            "owner": self._normalize_owner(identity),
            "request": normalized_request,
            "prompt_preview": str(normalized_request.get("prompt") or "")[:120],
            "base_url": _clean(base_url),
            "reserved_quota": max(0, int(reserved_quota or 0)),
            "refunded_quota": 0,
            "cost_units": 0,
            "attempts": 0,
            "max_attempts": max(1, _safe_int(normalized_request.get("max_attempts"), self.default_max_attempts)),
            "next_run_after": None,
            "dead_lettered_at": None,
            "result": None,
            "assets": [],
            "error": None,
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "completed_at": None,
        }
        with self._lock:
            self._jobs.append(job)
            self._save_job(job)
            if self.coordinator is not None:
                self.coordinator.enqueue(str(job.get("id") or ""))
            return self._public_job(job)

    def list_jobs(
        self,
        identity: dict[str, object] | None = None,
        *,
        limit: int = 50,
        status: str | None = None,
    ) -> list[dict[str, object]]:
        safe_limit = min(max(1, int(limit or 50)), 500)
        normalized_status = _clean(status)
        with self._lock:
            items = list(self._jobs)
            if normalized_status:
                items = [job for job in items if job.get("status") == normalized_status]
            if identity is not None and identity.get("role") != "admin":
                items = [job for job in items if self._can_access(identity, job)]
            items.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
            return [self._public_job(job, include_result=False) for job in items[:safe_limit]]

    def list_dead_letter_jobs(self, *, limit: int = 100) -> list[dict[str, object]]:
        safe_limit = min(max(1, int(limit or 100)), 500)
        with self._lock:
            if self.coordinator is not None:
                self._refresh_jobs_from_storage()
            items = [
                job
                for job in self._jobs
                if job.get("status") == "failed"
                and (
                    job.get("dead_lettered_at")
                    or (isinstance(job.get("error"), dict) and bool((job.get("error") or {}).get("dead_lettered")))
                )
            ]
            items.sort(key=lambda item: str(item.get("dead_lettered_at") or item.get("completed_at") or item.get("updated_at") or ""), reverse=True)
            return [self._public_job(job, include_result=False) for job in items[:safe_limit]]

    def get_job(self, job_id: str) -> dict[str, object] | None:
        normalized_id = _clean(job_id)
        with self._lock:
            for job in self._jobs:
                if job.get("id") == normalized_id:
                    return self._public_job(job)
        return None

    def get_job_for_identity(self, job_id: str, identity: dict[str, object]) -> dict[str, object] | None:
        normalized_id = _clean(job_id)
        with self._lock:
            for job in self._jobs:
                if job.get("id") == normalized_id and self._can_access(identity, job):
                    return self._public_job(job)
        return None

    def _refund_reserved_quota(self, job: dict[str, object], units: int, refund_reason: str) -> int:
        owner = job.get("owner") if isinstance(job.get("owner"), dict) else {}
        user_id = _clean(owner.get("user_id"))
        refundable = max(0, min(int(units or 0), int(job.get("reserved_quota") or 0) - int(job.get("refunded_quota") or 0)))
        if user_id and refundable > 0:
            self.auth_service.refund_user_quota(
                user_id,
                refundable,
                reason="refund",
                ref_type="image_job",
                ref_id=str(job.get("id") or ""),
                metadata={"refund_reason": refund_reason},
            )
            job["refunded_quota"] = int(job.get("refunded_quota") or 0) + refundable
        return refundable

    def cancel_job(self, job_id: str, identity: dict[str, object]) -> dict[str, object] | None:
        normalized_id = _clean(job_id)
        with self._lock:
            for index, job in enumerate(self._jobs):
                if job.get("id") != normalized_id or not self._can_access(identity, job):
                    continue
                if job.get("status") != "queued":
                    raise ValueError("only queued jobs can be cancelled")
                next_job = dict(job)
                next_job["status"] = "cancelled"
                next_job["updated_at"] = _now_iso()
                next_job["completed_at"] = next_job["updated_at"]
                self._refund_reserved_quota(next_job, int(next_job.get("reserved_quota") or 0), "cancelled")
                self._jobs[index] = next_job
                self._save_job(next_job)
                if self.coordinator is not None:
                    self.coordinator.complete(normalized_id)
                return self._public_job(next_job)
        return None

    def run_next(self, chatgpt_service: Any, base_url: str = "") -> dict[str, object] | None:
        claimed_job_id: str | None = None
        with self._lock:
            if self.coordinator is not None:
                self._refresh_jobs_from_storage()
            self._recover_stale_running_jobs_locked()
            queued_index = self._claim_next_queued_index()
            if queued_index is None:
                return None

            job = dict(self._jobs[queued_index])
            claimed_job_id = str(job.get("id") or "")
            job["status"] = "running"
            job["attempts"] = int(job.get("attempts") or 0) + 1
            job["started_at"] = _now_iso()
            job["updated_at"] = job["started_at"]
            job["error"] = None
            self._jobs[queued_index] = job
            self._save_job(job)

        request = job.get("request") if isinstance(job.get("request"), dict) else {}
        job_base_url = _clean(job.get("base_url")) or _clean(base_url)
        try:
            result = chatgpt_service.generate_with_pool(
                request.get("prompt"),
                request.get("model"),
                request.get("n"),
                request.get("size"),
                request.get("response_format"),
                job_base_url,
            )
            success_count = _count_success_items(result, _safe_int(request.get("n"), 1))
            with self._lock:
                index = self._find_index(str(job.get("id") or ""))
                if index is None:
                    return self._public_job(job)
                next_job = dict(self._jobs[index])
                assets = self.asset_service.archive_result(
                    owner=next_job.get("owner") if isinstance(next_job.get("owner"), dict) else {},
                    result=result,
                    job_id=str(next_job.get("id") or ""),
                    source="image.generation",
                    model=str(request.get("model") or ""),
                    prompt=str(request.get("prompt") or ""),
                    base_url=job_base_url,
                )
                reserved_quota = int(next_job.get("reserved_quota") or 0)
                if reserved_quota > success_count:
                    self._refund_reserved_quota(next_job, reserved_quota - success_count, "partial-success")
                if reserved_quota <= 0 and success_count > 0:
                    owner = next_job.get("owner") if isinstance(next_job.get("owner"), dict) else {}
                    if owner.get("role") != "admin" and not owner.get("user_id") and owner.get("key_id"):
                        self.auth_service.consume_quota(str(owner.get("key_id") or ""), success_count)
                now = _now_iso()
                next_job.update({
                    "status": "succeeded",
                    "result": result,
                    "assets": assets,
                    "error": None,
                    "cost_units": success_count,
                    "updated_at": now,
                    "completed_at": now,
                    "next_run_after": None,
                })
                self._jobs[index] = next_job
                self._save_job(next_job)
                if self.coordinator is not None:
                    self.coordinator.complete(str(next_job.get("id") or claimed_job_id or ""))
                return self._public_job(next_job)
        except Exception as exc:
            with self._lock:
                index = self._find_index(str(job.get("id") or ""))
                if index is None:
                    return self._public_job(job)
                next_job = dict(self._jobs[index])
                self._handle_job_failure(next_job, exc, failure_reason="failed")
                self._jobs[index] = next_job
                self._save_job(next_job)
                if self.coordinator is not None:
                    self.coordinator.complete(str(next_job.get("id") or claimed_job_id or ""))
                    if next_job.get("status") == "queued":
                        self.coordinator.requeue(str(next_job.get("id") or ""))
                return self._public_job(next_job)

    def recover_stale_running_jobs(self, *, stale_after_seconds: int | None = None) -> list[dict[str, object]]:
        with self._lock:
            if self.coordinator is not None:
                self._refresh_jobs_from_storage()
            recovered = self._recover_stale_running_jobs_locked(stale_after_seconds=stale_after_seconds)
            return [self._public_job(job, include_result=False) for job in recovered]

    def retry_dead_letter_job(
        self,
        job_id: str,
        *,
        actor: dict[str, object] | None = None,
        reason: str = "admin-retry",
    ) -> dict[str, object] | None:
        normalized_id = _clean(job_id)
        if not normalized_id:
            return None
        with self._lock:
            if self.coordinator is not None:
                self._refresh_jobs_from_storage()
            index = self._find_index(normalized_id)
            if index is None:
                return None
            job = dict(self._jobs[index])
            if job.get("status") != "failed":
                raise ValueError("only failed dead-letter jobs can be retried")
            if not job.get("dead_lettered_at") and not (isinstance(job.get("error"), dict) and bool((job.get("error") or {}).get("dead_lettered"))):
                raise ValueError("job is not in dead-letter state")

            self._reserve_retry_quota(job, actor=actor)
            previous_error = dict(job.get("error") or {}) if isinstance(job.get("error"), dict) else None
            now = _now_iso()
            job.update({
                "status": "queued",
                "attempts": 0,
                "result": None,
                "assets": [],
                "error": {
                    "message": _clean(reason) or "admin-retry",
                    "type": "AdminRetry",
                    "previous_error": previous_error,
                    "actor_id": (actor or {}).get("user_id") or (actor or {}).get("id"),
                },
                "updated_at": now,
                "started_at": None,
                "completed_at": None,
                "next_run_after": now,
                "dead_lettered_at": None,
            })
            self._jobs[index] = job
            self._save_job(job)
            if self.coordinator is not None:
                self.coordinator.complete(normalized_id)
                self.coordinator.requeue(normalized_id)
            return self._public_job(job)

    def _reserve_retry_quota(self, job: dict[str, object], *, actor: dict[str, object] | None = None) -> None:
        owner = job.get("owner") if isinstance(job.get("owner"), dict) else {}
        user_id = _clean(owner.get("user_id"))
        reserved_quota = max(0, int(job.get("reserved_quota") or 0))
        refunded_quota = max(0, int(job.get("refunded_quota") or 0))
        to_reserve = min(reserved_quota, refunded_quota)
        if not user_id or to_reserve <= 0:
            return
        ok = self.auth_service.try_consume_user_quota(
            user_id,
            to_reserve,
            reason="image-job-retry-reserve",
            ref_type="image_job",
            ref_id=str(job.get("id") or ""),
            actor_type=str((actor or {}).get("role") or "admin"),
            actor_id=str((actor or {}).get("user_id") or (actor or {}).get("id") or "admin"),
            metadata={"retry": True},
        )
        if not ok:
            raise ValueError("user quota is insufficient for retry")
        job["refunded_quota"] = max(0, refunded_quota - to_reserve)

    def _handle_job_failure(self, job: dict[str, object], exc: Exception, *, failure_reason: str) -> None:
        attempts = int(job.get("attempts") or 0)
        max_attempts = max(1, int(job.get("max_attempts") or self.default_max_attempts))
        error = {"message": str(exc), "type": exc.__class__.__name__, "attempt": attempts, "max_attempts": max_attempts}
        now_dt = _now()
        now = now_dt.isoformat()
        if attempts < max_attempts:
            next_run_after = (now_dt + timedelta(seconds=self.retry_delay_seconds)).isoformat()
            job.update({
                "status": "queued",
                "result": None,
                "error": error,
                "updated_at": now,
                "started_at": None,
                "completed_at": None,
                "next_run_after": next_run_after,
            })
            return

        self._refund_reserved_quota(job, int(job.get("reserved_quota") or 0), failure_reason)
        job.update({
            "status": "failed",
            "result": None,
            "error": {**error, "dead_lettered": True},
            "updated_at": now,
            "completed_at": now,
            "next_run_after": None,
            "dead_lettered_at": now,
        })
        if self.coordinator is not None:
            self.coordinator.dead_letter(str(job.get("id") or ""), failure_reason)

    def _recover_stale_running_jobs_locked(self, *, stale_after_seconds: int | None = None) -> list[dict[str, object]]:
        stale_seconds = max(1, int(stale_after_seconds or self.stale_running_seconds))
        cutoff = _now() - timedelta(seconds=stale_seconds)
        recovered: list[dict[str, object]] = []
        for index, job in enumerate(list(self._jobs)):
            if job.get("status") != "running":
                continue
            started_at = _parse_datetime(job.get("started_at") or job.get("updated_at"))
            if started_at is None or started_at > cutoff:
                continue
            next_job = dict(job)
            attempts = int(next_job.get("attempts") or 0)
            max_attempts = max(1, int(next_job.get("max_attempts") or self.default_max_attempts))
            now = _now_iso()
            if attempts < max_attempts:
                next_job.update({
                    "status": "queued",
                    "error": {
                        "message": f"stale running job recovered after {stale_seconds}s",
                        "type": "StaleRunningJob",
                        "attempt": attempts,
                        "max_attempts": max_attempts,
                    },
                    "updated_at": now,
                    "started_at": None,
                    "next_run_after": now,
                })
                if self.coordinator is not None:
                    self.coordinator.complete(str(next_job.get("id") or ""))
                    self.coordinator.requeue(str(next_job.get("id") or ""))
            else:
                class StaleRunningJobError(RuntimeError):
                    pass

                self._handle_job_failure(next_job, StaleRunningJobError(f"stale running job exceeded {stale_seconds}s"), failure_reason="stale-running")
                if self.coordinator is not None:
                    self.coordinator.complete(str(next_job.get("id") or ""))
            self._jobs[index] = next_job
            self._save_job(next_job)
            recovered.append(next_job)
        return recovered

    def _claim_next_queued_index(self) -> int | None:
        if self.coordinator is not None:
            for _ in range(100):
                queued_id = self.coordinator.pop_queued_id()
                if not queued_id:
                    break
                index = self._find_index(queued_id)
                if index is None:
                    self._refresh_jobs_from_storage()
                    index = self._find_index(queued_id)
                if index is None:
                    self.coordinator.complete(queued_id)
                    continue
                if self._jobs[index].get("status") != "queued":
                    self.coordinator.complete(queued_id)
                    continue
                if not self._job_is_due(self._jobs[index]):
                    self.coordinator.requeue(queued_id)
                    return None
                if self.coordinator.try_claim(queued_id):
                    return index
            for index, job in enumerate(self._jobs):
                if job.get("status") != "queued":
                    continue
                if not self._job_is_due(job):
                    continue
                job_id = str(job.get("id") or "")
                if self.coordinator.try_claim(job_id):
                    return index
            return None

        for index, job in enumerate(self._jobs):
            if job.get("status") == "queued" and self._job_is_due(job):
                return index
        return None

    @staticmethod
    def _job_is_due(job: dict[str, object]) -> bool:
        next_run_after = _parse_datetime(job.get("next_run_after"))
        return next_run_after is None or next_run_after <= _now()

    def _find_index(self, job_id: str) -> int | None:
        for index, job in enumerate(self._jobs):
            if job.get("id") == job_id:
                return index
        return None


def start_image_job_worker(
    stop_event: Event,
    job_service: ImageJobService,
    chatgpt_service: Any,
    *,
    base_url: str = "",
) -> Thread:
    interval = max(0.2, float(os.getenv("IMAGE_JOB_WORKER_INTERVAL_SECONDS", "1") or "1"))

    def worker() -> None:
        while not stop_event.is_set():
            try:
                processed = job_service.run_next(chatgpt_service, base_url)
                if processed is None:
                    stop_event.wait(interval)
            except Exception as exc:
                print(f"[image-job-worker] fail {exc}")
                stop_event.wait(interval)

    thread = Thread(target=worker, name="image-job-worker", daemon=True)
    thread.start()
    return thread


image_job_service = ImageJobService(config.get_storage_backend(), auth_service, coordinator=create_image_job_coordinator_from_env())
