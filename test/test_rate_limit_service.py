from __future__ import annotations

import unittest

from services.rate_limit_service import InMemoryRateLimiter, RedisRateLimiter


class FakeRedis:
    def __init__(self):
        self.values: dict[str, int] = {}
        self.expirations: dict[str, int] = {}
        self.deleted: list[str] = []

    def incrby(self, key: str, amount: int):
        self.values[key] = self.values.get(key, 0) + amount
        return self.values[key]

    def expire(self, key: str, seconds: int):
        self.expirations[key] = seconds
        return True

    def ttl(self, key: str):
        return self.expirations.get(key, 60)

    def delete(self, key: str):
        self.deleted.append(key)
        self.values.pop(key, None)
        return 1

    def ping(self):
        return True


class RateLimitServiceTests(unittest.TestCase):
    def test_in_memory_limiter_uses_external_bucket_and_clear(self):
        bucket: dict[str, list[float]] = {}
        limiter = InMemoryRateLimiter(bucket=bucket)

        first = limiter.allow("login:user@example.com", 2)
        second = limiter.allow("login:user@example.com", 2)
        third = limiter.allow("login:user@example.com", 2)

        self.assertTrue(first.allowed)
        self.assertTrue(second.allowed)
        self.assertFalse(third.allowed)
        self.assertEqual(len(bucket["login:user@example.com"]), 2)

        limiter.clear("login:user@example.com")
        self.assertNotIn("login:user@example.com", bucket)
        self.assertTrue(limiter.allow("login:user@example.com", 2).allowed)

    def test_redis_limiter_blocks_over_fixed_window_limit(self):
        fake = FakeRedis()
        limiter = RedisRateLimiter(fake, prefix="test:rate")

        self.assertTrue(limiter.allow("api:key-1", 2, cost=2).allowed)
        blocked = limiter.allow("api:key-1", 2, cost=1)

        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.backend, "redis")
        self.assertEqual(blocked.retry_after_seconds, 61)
        self.assertEqual(limiter.info()["healthy"], True)


if __name__ == "__main__":
    unittest.main()
