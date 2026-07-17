from __future__ import annotations

import os
import time
from dataclasses import dataclass
from threading import Lock
from typing import Any, Mapping


def _clean(value: object) -> str:
    return str(value or "").strip()


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _decode(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return _clean(value)


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int
    backend: str


class InMemoryRateLimiter:
    """Process-local sliding-window limiter.

    This is safe for local development and single-process deployments. For
    multi-process or multi-replica public launch, use `RATE_LIMIT_BACKEND=redis`.
    """

    backend = "memory"

    def __init__(self, *, bucket: dict[str, list[float]] | None = None):
        self.bucket = bucket if bucket is not None else {}
        self._lock = Lock()

    def allow(self, key: str, limit: int, *, window_seconds: int = 60, cost: int = 1) -> RateLimitResult:
        safe_limit = max(0, int(limit or 0))
        safe_cost = max(1, int(cost or 1))
        safe_window = max(1, int(window_seconds or 60))
        if safe_limit <= 0:
            return RateLimitResult(True, safe_limit, safe_limit, 0, self.backend)
        now = time.time()
        with self._lock:
            recent = [timestamp for timestamp in self.bucket.get(key, []) if now - timestamp < safe_window]
            if len(recent) + safe_cost > safe_limit:
                oldest = min(recent) if recent else now
                retry_after = max(1, int(safe_window - (now - oldest)) + 1)
                self.bucket[key] = recent
                return RateLimitResult(False, safe_limit, max(0, safe_limit - len(recent)), retry_after, self.backend)
            recent.extend([now] * safe_cost)
            self.bucket[key] = recent
            return RateLimitResult(True, safe_limit, max(0, safe_limit - len(recent)), 0, self.backend)

    def clear(self, key: str) -> None:
        with self._lock:
            self.bucket.pop(key, None)

    def info(self) -> dict[str, object]:
        with self._lock:
            keys = len(self.bucket)
        return {"backend": self.backend, "tracked_keys": keys}


class RedisRateLimiter:
    """Redis fixed-window limiter shared across API replicas."""

    backend = "redis"

    def __init__(self, client: Any, *, prefix: str = "chatgpt2api:rate_limit:"):
        self.client = client
        self.prefix = _clean(prefix) or "chatgpt2api:rate_limit:"
        if not self.prefix.endswith(":"):
            self.prefix = f"{self.prefix}:"

    def _key(self, key: str, window_index: int) -> str:
        normalized = _clean(key).replace("\n", "_").replace("\r", "_")
        return f"{self.prefix}{normalized}:{window_index}"

    def allow(self, key: str, limit: int, *, window_seconds: int = 60, cost: int = 1) -> RateLimitResult:
        safe_limit = max(0, int(limit or 0))
        safe_cost = max(1, int(cost or 1))
        safe_window = max(1, int(window_seconds or 60))
        if safe_limit <= 0:
            return RateLimitResult(True, safe_limit, safe_limit, 0, self.backend)
        now = int(time.time())
        window_index = now // safe_window
        redis_key = self._key(key, window_index)
        current = int(self.client.incrby(redis_key, safe_cost))
        if current == safe_cost:
            self.client.expire(redis_key, safe_window + 1)
        retry_after = max(1, ((window_index + 1) * safe_window) - now)
        if current > safe_limit:
            try:
                ttl = int(self.client.ttl(redis_key))
                if ttl > 0:
                    retry_after = ttl
            except Exception:
                pass
            return RateLimitResult(False, safe_limit, 0, retry_after, self.backend)
        return RateLimitResult(True, safe_limit, max(0, safe_limit - current), 0, self.backend)

    def clear(self, key: str) -> None:
        now = int(time.time())
        current_index = now // 60
        # Clear the common current/previous/next 60s buckets used by auth
        # flows. This keeps successful login reset behavior deterministic
        # without scanning Redis keys.
        for window_index in (current_index - 1, current_index, current_index + 1):
            self.client.delete(self._key(key, window_index))

    def info(self) -> dict[str, object]:
        ok = False
        error = ""
        try:
            response = self.client.ping()
            ok = bool(response)
        except Exception as exc:
            error = str(exc)
        return {"backend": self.backend, "prefix": self.prefix, "healthy": ok, "error": error}


def create_rate_limiter_from_env(
    *,
    namespace: str = "default",
    memory_bucket: dict[str, list[float]] | None = None,
    env: Mapping[str, str] | None = None,
) -> InMemoryRateLimiter | RedisRateLimiter:
    source = env if env is not None else os.environ
    backend = _clean(source.get("RATE_LIMIT_BACKEND") or "memory").lower()
    if backend == "auto":
        backend = "redis" if _clean(source.get("REDIS_URL")) else "memory"
    if backend in {"", "memory", "local", "in-memory"}:
        return InMemoryRateLimiter(bucket=memory_bucket)
    if backend != "redis":
        raise ValueError(f"unsupported RATE_LIMIT_BACKEND: {backend}")
    try:
        import redis  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("redis package is required when RATE_LIMIT_BACKEND=redis") from exc

    redis_url = _clean(source.get("RATE_LIMIT_REDIS_URL") or source.get("REDIS_URL")) or "redis://localhost:6379/0"
    prefix = _clean(source.get("RATE_LIMIT_REDIS_PREFIX") or "chatgpt2api:rate_limit")
    namespace_value = _clean(namespace) or "default"
    client = redis.Redis.from_url(redis_url, decode_responses=True)
    return RedisRateLimiter(client, prefix=f"{prefix}:{namespace_value}")


rate_limit_service = create_rate_limiter_from_env(namespace="default")
