import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.jobs as jobs_api
from services.auth_service import AuthService
from services.image_asset_service import ImageAssetService
from services.image_job_service import ImageJobService
from services.storage.json_storage import JSONStorageBackend

ONE_PIXEL_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


class StubChatGPTService:
    def __init__(self, result=None, error: Exception | None = None):
        self.result = result or {"data": [{"b64_json": ONE_PIXEL_PNG_B64}]}
        self.error = error
        self.calls = []

    def generate_with_pool(self, *args):
        self.calls.append(args)
        if self.error:
            raise self.error
        return self.result


class ImageJobApiTests(unittest.TestCase):
    def create_services(self, chatgpt_service: StubChatGPTService | None = None):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        base_dir = Path(tmp_dir.name)
        storage = JSONStorageBackend(base_dir / "accounts.json", base_dir / "auth_keys.json")
        auth = AuthService(storage)
        assets = ImageAssetService(storage, base_dir / "assets")
        jobs = ImageJobService(storage, auth, assets)
        app = FastAPI()
        app.include_router(jobs_api.create_router(chatgpt_service or StubChatGPTService(), jobs))
        return TestClient(app), auth, jobs

    def patch_auth(self, auth: AuthService):
        return patch.multiple(
            jobs_api,
            auth_service=auth,
        ), patch.dict(jobs_api.require_permission.__globals__, {"auth_service": auth})

    def test_image_job_success_consumes_reserved_quota(self):
        client, auth, _ = self.create_services()
        user, token, _ = auth.register_user("user@example.com", "StrongPass123")
        admin_key = auth.create_key(role="admin", name="Admin")[1]
        auth.adjust_user_quota(str(user["id"]), 1, "seed")

        patches = self.patch_auth(auth)
        with patches[0], patches[1]:
            enqueue_response = client.post(
                "/api/jobs/images/generations",
                headers={"Authorization": f"Bearer {token}"},
                json={"prompt": "cat", "n": 1},
            )
            self.assertEqual(enqueue_response.status_code, 202)
            job_id = enqueue_response.json()["job"]["id"]
            self.assertEqual(enqueue_response.json()["job"]["status"], "queued")
            self.assertEqual(auth.list_users()[0]["quota_balance"], 0)

            list_response = client.get("/api/jobs?status=queued", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(list_response.status_code, 200)
            self.assertEqual(len(list_response.json()["items"]), 1)

            run_response = client.post("/api/admin/jobs/run-next", headers={"Authorization": f"Bearer {admin_key}"})
            self.assertEqual(run_response.status_code, 200)
            self.assertEqual(run_response.json()["job"]["status"], "succeeded")

            get_response = client.get(f"/api/jobs/{job_id}", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(get_response.status_code, 200)
            self.assertEqual(len(get_response.json()["job"]["assets"]), 1)
            self.assertTrue(get_response.json()["job"]["assets"][0]["url"].startswith("http://testserver/assets/"))

        ledger = list(reversed(auth.list_quota_ledger(str(user["id"]))))
        self.assertEqual([item["amount"] for item in ledger], [1, -1])
        self.assertEqual(ledger[1]["ref_type"], "image_job")
        self.assertEqual(ledger[1]["ref_id"], job_id)

    def test_image_job_failure_refunds_reserved_quota(self):
        client, auth, _ = self.create_services(StubChatGPTService(error=RuntimeError("upstream failed")))
        user, token, _ = auth.register_user("user@example.com", "StrongPass123")
        admin_key = auth.create_key(role="admin", name="Admin")[1]
        auth.adjust_user_quota(str(user["id"]), 1, "seed")

        patches = self.patch_auth(auth)
        with patches[0], patches[1]:
            enqueue_response = client.post(
                "/api/jobs/images/generations",
                headers={"Authorization": f"Bearer {token}"},
                json={"prompt": "cat", "n": 1},
            )
            self.assertEqual(enqueue_response.status_code, 202)
            job_id = enqueue_response.json()["job"]["id"]
            self.assertEqual(auth.list_users()[0]["quota_balance"], 0)

            run_response = client.post("/api/admin/jobs/run-next", headers={"Authorization": f"Bearer {admin_key}"})
            self.assertEqual(run_response.status_code, 200)
            self.assertEqual(run_response.json()["job"]["status"], "failed")
            self.assertEqual(run_response.json()["job"]["error"]["message"], "upstream failed")
            self.assertEqual(auth.list_users()[0]["quota_balance"], 1)

            dead_letter_response = client.get("/api/admin/jobs/dead-letter", headers={"Authorization": f"Bearer {admin_key}"})
            self.assertEqual(dead_letter_response.status_code, 200)
            self.assertEqual(dead_letter_response.json()["items"][0]["id"], job_id)

            retry_response = client.post(f"/api/admin/jobs/{job_id}/retry", headers={"Authorization": f"Bearer {admin_key}"}, json={"reason": "retry"})
            self.assertEqual(retry_response.status_code, 200)
            self.assertEqual(retry_response.json()["job"]["status"], "queued")
            self.assertEqual(auth.list_users()[0]["quota_balance"], 0)

        ledger = list(reversed(auth.list_quota_ledger(str(user["id"]))))
        self.assertEqual([item["type"] for item in ledger], ["grant", "consume", "refund", "consume"])
        self.assertEqual([item["amount"] for item in ledger], [1, -1, 1, -1])
        self.assertEqual(ledger[2]["ref_id"], job_id)

    def test_cancel_queued_image_job_refunds_reserved_quota(self):
        client, auth, _ = self.create_services()
        user, token, _ = auth.register_user("user@example.com", "StrongPass123")
        admin_key = auth.create_key(role="admin", name="Admin")[1]
        auth.adjust_user_quota(str(user["id"]), 1, "seed")

        patches = self.patch_auth(auth)
        with patches[0], patches[1]:
            enqueue_response = client.post(
                "/api/jobs/images/generations",
                headers={"Authorization": f"Bearer {token}"},
                json={"prompt": "cat", "n": 1},
            )
            self.assertEqual(enqueue_response.status_code, 202)
            job_id = enqueue_response.json()["job"]["id"]

            cancel_response = client.post(f"/api/jobs/{job_id}/cancel", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(cancel_response.status_code, 200)
            self.assertEqual(cancel_response.json()["job"]["status"], "cancelled")
            self.assertEqual(auth.list_users()[0]["quota_balance"], 1)

            run_response = client.post("/api/admin/jobs/run-next", headers={"Authorization": f"Bearer {admin_key}"})
            self.assertEqual(run_response.status_code, 200)
            self.assertIsNone(run_response.json()["job"])


if __name__ == "__main__":
    unittest.main()
