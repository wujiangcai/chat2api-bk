from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.auth_service import AuthService
from services.image_asset_service import ImageAssetService
from services.image_job_queue import RedisImageJobCoordinator
from services.image_job_service import ImageJobService
from services.storage.json_storage import JSONStorageBackend

ONE_PIXEL_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


class FakeRedis:
    def __init__(self):
        self.lists: dict[str, list[str]] = {}
        self.values: dict[str, str] = {}

    def rpush(self, key: str, value: str):
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    def lpop(self, key: str):
        items = self.lists.setdefault(key, [])
        if not items:
            return None
        return items.pop(0)

    def set(self, key: str, value: str, nx: bool = False, ex: int | None = None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def delete(self, key: str):
        return 1 if self.values.pop(key, None) is not None else 0

    def llen(self, key: str):
        return len(self.lists.get(key, []))


class StubChatGPTService:
    def __init__(self):
        self.calls = 0

    def generate_with_pool(self, *args):
        self.calls += 1
        return {"data": [{"b64_json": ONE_PIXEL_PNG_B64}]}


class FlakyChatGPTService:
    def __init__(self, failures: int):
        self.failures = failures
        self.calls = 0

    def generate_with_pool(self, *args):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError(f"temporary failure {self.calls}")
        return {"data": [{"b64_json": ONE_PIXEL_PNG_B64}]}


class ImageJobQueueTests(unittest.TestCase):
    def create_services(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        base_dir = Path(tmp_dir.name)
        storage = JSONStorageBackend(base_dir / "accounts.json", base_dir / "auth_keys.json")
        auth = AuthService(storage)
        assets = ImageAssetService(storage, base_dir / "assets")
        redis = FakeRedis()
        coordinator = RedisImageJobCoordinator(redis, queue_key="test:queued", dead_letter_key="test:dead", lock_prefix="test:lock:")
        return storage, auth, assets, coordinator, redis

    def test_redis_coordinator_allows_worker_instance_to_pick_job_created_elsewhere(self):
        storage, auth, assets, coordinator, redis = self.create_services()
        creator_service = ImageJobService(storage, auth, assets, coordinator=coordinator)
        worker_service = ImageJobService(storage, auth, assets, coordinator=coordinator)
        chatgpt = StubChatGPTService()

        user, _, key = auth.register_user("user@example.com", "StrongPass123")
        auth.adjust_user_quota(str(user["id"]), 1, "seed")
        auth.try_consume_user_quota(str(user["id"]), 1, ref_type="image_job", ref_id="job_shared")

        creator_service.enqueue_generation(
            job_id="job_shared",
            identity={**key, **user, "user_id": user["id"], "key_id": key["id"]},
            request={"prompt": "cat", "n": 1, "response_format": "b64_json"},
            reserved_quota=1,
        )
        self.assertEqual(redis.llen("test:queued"), 1)

        processed = worker_service.run_next(chatgpt, "http://testserver")
        self.assertIsNotNone(processed)
        self.assertEqual(processed["status"], "succeeded")
        self.assertEqual(chatgpt.calls, 1)
        self.assertEqual(redis.llen("test:queued"), 0)
        self.assertFalse(redis.values)

        reloaded = ImageJobService(storage, auth, assets, coordinator=coordinator)
        self.assertEqual(reloaded.get_job("job_shared")["status"], "succeeded")

    def test_redis_claim_prevents_duplicate_processing(self):
        storage, auth, assets, coordinator, _ = self.create_services()
        service = ImageJobService(storage, auth, assets, coordinator=coordinator)
        chatgpt = StubChatGPTService()
        user, _, key = auth.register_user("user@example.com", "StrongPass123")

        service.enqueue_generation(
            job_id="job_locked",
            identity={**key, **user, "user_id": user["id"], "key_id": key["id"]},
            request={"prompt": "cat", "n": 1, "response_format": "b64_json"},
        )
        self.assertTrue(coordinator.try_claim("job_locked"))

        self.assertIsNone(service.run_next(chatgpt, "http://testserver"))
        self.assertEqual(chatgpt.calls, 0)

        coordinator.complete("job_locked")
        processed = service.run_next(chatgpt, "http://testserver")
        self.assertEqual(processed["status"], "succeeded")
        self.assertEqual(chatgpt.calls, 1)

    def test_retry_requeues_transient_failure_before_final_dead_letter(self):
        storage, auth, assets, coordinator, redis = self.create_services()
        service = ImageJobService(storage, auth, assets, coordinator=coordinator, default_max_attempts=2, retry_delay_seconds=0)
        chatgpt = FlakyChatGPTService(failures=1)
        user, _, key = auth.register_user("user@example.com", "StrongPass123")

        service.enqueue_generation(
            job_id="job_retry",
            identity={**key, **user, "user_id": user["id"], "key_id": key["id"]},
            request={"prompt": "cat", "n": 1, "response_format": "b64_json"},
        )

        first_attempt = service.run_next(chatgpt, "http://testserver")
        self.assertEqual(first_attempt["status"], "queued")
        self.assertEqual(first_attempt["attempts"], 1)
        self.assertEqual(redis.llen("test:queued"), 1)

        second_attempt = service.run_next(chatgpt, "http://testserver")
        self.assertEqual(second_attempt["status"], "succeeded")
        self.assertEqual(second_attempt["attempts"], 2)
        self.assertEqual(chatgpt.calls, 2)
        self.assertEqual(redis.llen("test:queued"), 0)

    def test_final_failure_is_dead_lettered_after_max_attempts(self):
        storage, auth, assets, coordinator, redis = self.create_services()
        service = ImageJobService(storage, auth, assets, coordinator=coordinator, default_max_attempts=1, retry_delay_seconds=0)
        chatgpt = FlakyChatGPTService(failures=99)
        user, _, key = auth.register_user("user@example.com", "StrongPass123")

        service.enqueue_generation(
            job_id="job_dead",
            identity={**key, **user, "user_id": user["id"], "key_id": key["id"]},
            request={"prompt": "cat", "n": 1, "response_format": "b64_json"},
        )

        result = service.run_next(chatgpt, "http://testserver")
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["error"]["dead_lettered"])
        self.assertEqual(redis.llen("test:queued"), 0)
        self.assertEqual(redis.llen("test:dead"), 1)

    def test_stale_running_job_is_recovered_to_queue(self):
        storage, auth, assets, coordinator, redis = self.create_services()
        service = ImageJobService(storage, auth, assets, coordinator=coordinator, default_max_attempts=2, retry_delay_seconds=0)
        user, _, key = auth.register_user("user@example.com", "StrongPass123")
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        stale_job = service.enqueue_generation(
            job_id="job_stale",
            identity={**key, **user, "user_id": user["id"], "key_id": key["id"]},
            request={"prompt": "cat", "n": 1, "response_format": "b64_json"},
        )
        raw_job = storage.load_collection("image_jobs")[0]
        raw_job.update({"status": "running", "attempts": 1, "started_at": old_time, "updated_at": old_time})
        storage.save_collection("image_jobs", [raw_job])

        recovered = service.recover_stale_running_jobs(stale_after_seconds=1)
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["id"], stale_job["id"])
        self.assertEqual(recovered[0]["status"], "queued")
        self.assertGreaterEqual(redis.llen("test:queued"), 1)

    def test_retry_dead_letter_re_reserves_refunded_user_quota(self):
        storage, auth, assets, coordinator, redis = self.create_services()
        service = ImageJobService(storage, auth, assets, coordinator=coordinator, default_max_attempts=1, retry_delay_seconds=0)
        chatgpt = FlakyChatGPTService(failures=99)
        user, _, key = auth.register_user("user@example.com", "StrongPass123")
        auth.adjust_user_quota(str(user["id"]), 2, "seed")
        self.assertTrue(auth.try_consume_user_quota(str(user["id"]), 1, ref_type="image_job", ref_id="job_retry_quota"))

        service.enqueue_generation(
            job_id="job_retry_quota",
            identity={**key, **user, "user_id": user["id"], "key_id": key["id"]},
            request={"prompt": "cat", "n": 1, "response_format": "b64_json"},
            reserved_quota=1,
        )

        failed = service.run_next(chatgpt, "http://testserver")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["refunded_quota"], 1)
        self.assertEqual(auth.list_users()[0]["quota_balance"], 2)

        retried = service.retry_dead_letter_job("job_retry_quota", actor={"role": "admin", "id": "admin"})
        self.assertEqual(retried["status"], "queued")
        self.assertEqual(retried["attempts"], 0)
        self.assertEqual(retried["refunded_quota"], 0)
        self.assertIsNone(retried["dead_lettered_at"])
        self.assertEqual(auth.list_users()[0]["quota_balance"], 1)
        self.assertGreaterEqual(redis.llen("test:queued"), 1)

        ledger = list(reversed(auth.list_quota_ledger(str(user["id"]))))
        self.assertEqual([item["amount"] for item in ledger], [2, -1, 1, -1])
        self.assertEqual(ledger[-1]["reason"], "image-job-retry-reserve")


if __name__ == "__main__":
    unittest.main()
