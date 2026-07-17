from __future__ import annotations

import os
import socket
import uuid
from typing import Any, Protocol


def _clean(value: object) -> str:
    return str(value or "").strip()


def _decode(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return _clean(value)


class ImageJobCoordinator(Protocol):
    """Queue/lock coordinator used by image workers.

    The storage table remains the source of truth for job metadata. A
    coordinator adds production-grade scheduling and distributed claiming so
    multiple API/worker processes do not execute the same queued job.
    """

    def enqueue(self, job_id: str) -> None:
        ...

    def pop_queued_id(self) -> str | None:
        ...

    def try_claim(self, job_id: str) -> bool:
        ...

    def complete(self, job_id: str) -> None:
        ...

    def requeue(self, job_id: str) -> None:
        ...

    def dead_letter(self, job_id: str, reason: str = "") -> None:
        ...

    def info(self) -> dict[str, object]:
        ...


class RedisImageJobCoordinator:
    """Redis-backed queue + distributed lock for image jobs.

    Redis is an accelerator and lock provider; durable job data still lives in
    the configured storage backend. If a queued id is missing from Redis, the
    worker can still fall back to scanning persisted queued jobs and claiming
    one by lock.
    """

    def __init__(
        self,
        client: Any,
        *,
        queue_key: str = "chatgpt2api:image_jobs:queued",
        dead_letter_key: str = "chatgpt2api:image_jobs:dead_letter",
        lock_prefix: str = "chatgpt2api:image_jobs:lock:",
        lock_ttl_seconds: int = 900,
        owner_id: str | None = None,
    ):
        self.client = client
        self.queue_key = _clean(queue_key) or "chatgpt2api:image_jobs:queued"
        self.dead_letter_key = _clean(dead_letter_key) or "chatgpt2api:image_jobs:dead_letter"
        self.lock_prefix = _clean(lock_prefix) or "chatgpt2api:image_jobs:lock:"
        self.lock_ttl_seconds = max(30, int(lock_ttl_seconds or 900))
        self.owner_id = _clean(owner_id) or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

    def _lock_key(self, job_id: str) -> str:
        return f"{self.lock_prefix}{_clean(job_id)}"

    def enqueue(self, job_id: str) -> None:
        normalized_id = _clean(job_id)
        if normalized_id:
            self.client.rpush(self.queue_key, normalized_id)

    def pop_queued_id(self) -> str | None:
        value = self.client.lpop(self.queue_key)
        queued_id = _decode(value)
        return queued_id or None

    def try_claim(self, job_id: str) -> bool:
        normalized_id = _clean(job_id)
        if not normalized_id:
            return False
        result = self.client.set(
            self._lock_key(normalized_id),
            self.owner_id,
            nx=True,
            ex=self.lock_ttl_seconds,
        )
        return bool(result)

    def complete(self, job_id: str) -> None:
        normalized_id = _clean(job_id)
        if normalized_id:
            self.client.delete(self._lock_key(normalized_id))

    def requeue(self, job_id: str) -> None:
        self.enqueue(job_id)

    def dead_letter(self, job_id: str, reason: str = "") -> None:
        normalized_id = _clean(job_id)
        if normalized_id:
            payload = f"{normalized_id}|{_clean(reason)}" if _clean(reason) else normalized_id
            self.client.rpush(self.dead_letter_key, payload)

    def info(self) -> dict[str, object]:
        queued_count: int | None = None
        dead_letter_count: int | None = None
        try:
            queued_count = int(self.client.llen(self.queue_key))
        except Exception:
            queued_count = None
        try:
            dead_letter_count = int(self.client.llen(self.dead_letter_key))
        except Exception:
            dead_letter_count = None
        return {
            "backend": "redis",
            "queue_key": self.queue_key,
            "dead_letter_key": self.dead_letter_key,
            "lock_prefix": self.lock_prefix,
            "lock_ttl_seconds": self.lock_ttl_seconds,
            "queued_count": queued_count,
            "dead_letter_count": dead_letter_count,
        }


def create_image_job_coordinator_from_env() -> ImageJobCoordinator | None:
    backend = _clean(os.getenv("IMAGE_JOB_QUEUE_BACKEND") or os.getenv("IMAGE_JOB_COORDINATOR_BACKEND") or "storage").lower()
    if backend in {"", "storage", "polling", "none", "local"}:
        return None
    if backend != "redis":
        raise ValueError(f"unsupported IMAGE_JOB_QUEUE_BACKEND: {backend}")

    try:
        import redis  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("redis package is required when IMAGE_JOB_QUEUE_BACKEND=redis") from exc

    redis_url = _clean(os.getenv("REDIS_URL")) or "redis://localhost:6379/0"
    client = redis.Redis.from_url(redis_url, decode_responses=True)
    return RedisImageJobCoordinator(
        client,
        queue_key=_clean(os.getenv("IMAGE_JOB_REDIS_QUEUE_KEY")) or "chatgpt2api:image_jobs:queued",
        dead_letter_key=_clean(os.getenv("IMAGE_JOB_REDIS_DEAD_LETTER_KEY")) or "chatgpt2api:image_jobs:dead_letter",
        lock_prefix=_clean(os.getenv("IMAGE_JOB_REDIS_LOCK_PREFIX")) or "chatgpt2api:image_jobs:lock:",
        lock_ttl_seconds=int(os.getenv("IMAGE_JOB_REDIS_LOCK_TTL_SECONDS", "900") or "900"),
    )
