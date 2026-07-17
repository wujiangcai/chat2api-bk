import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

class ImageGenerationError(Exception):
    pass

fake_chatgpt_service = types.ModuleType("services.chatgpt_service")
fake_chatgpt_service.ChatGPTService = object
fake_chatgpt_service.ImageGenerationError = ImageGenerationError
_original_chatgpt_service_module = sys.modules.get("services.chatgpt_service")
sys.modules["services.chatgpt_service"] = fake_chatgpt_service

from fastapi import FastAPI
from fastapi.testclient import TestClient

AI_PATH = Path(__file__).resolve().parents[1] / "api" / "ai.py"
AI_SPEC = importlib.util.spec_from_file_location("ai_under_test", AI_PATH)
ai_module = importlib.util.module_from_spec(AI_SPEC)
assert AI_SPEC and AI_SPEC.loader
AI_SPEC.loader.exec_module(ai_module)
if _original_chatgpt_service_module is None:
    sys.modules.pop("services.chatgpt_service", None)
else:
    sys.modules["services.chatgpt_service"] = _original_chatgpt_service_module
create_router = ai_module.create_router

from services.auth_service import AuthService
from services.image_asset_service import ImageAssetService
from services.storage.json_storage import JSONStorageBackend

ONE_PIXEL_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


class StubChatGPTService:
    def __init__(self, result=None, error=None):
        self.result = result or {"data": [{"url": "https://example.com/image.png"}]}
        self.error = error

    def list_models(self):
        return {"data": []}

    def generate_with_pool(self, *args, **kwargs):
        if self.error:
            raise self.error
        return self.result

    def edit_with_pool(self, *args, **kwargs):
        if self.error:
            raise self.error
        return self.result


class ImageQuotaRouteTests(unittest.TestCase):
    def create_auth(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        base_dir = Path(tmp_dir.name)
        storage = JSONStorageBackend(base_dir / "accounts.json", base_dir / "auth_keys.json")
        return AuthService(storage)

    def create_storage_services(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        base_dir = Path(tmp_dir.name)
        storage = JSONStorageBackend(base_dir / "accounts.json", base_dir / "auth_keys.json")
        return AuthService(storage), ImageAssetService(storage, base_dir / "assets")

    def create_client(self, auth_service, service):
        app = FastAPI()
        app.include_router(create_router(service))
        return TestClient(app)

    def test_generation_reserves_quota_on_success(self):
        auth = self.create_auth()
        user, token, _ = auth.register_user("user@example.com", "StrongPass123")
        auth.adjust_user_quota(str(user["id"]), 1)
        client = self.create_client(auth, StubChatGPTService())

        with patch.dict(ai_module.require_permission.__globals__, {"auth_service": auth}):
            response = client.post(
                "/v1/images/generations",
                headers={"Authorization": f"Bearer {token}"},
                json={"prompt": "cat", "n": 1},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(auth.list_users()[0]["quota_balance"], 0)

    def test_generation_archives_successful_image_asset(self):
        auth, assets = self.create_storage_services()
        user, token, _ = auth.register_user("user@example.com", "StrongPass123")
        auth.adjust_user_quota(str(user["id"]), 1)
        client = self.create_client(auth, StubChatGPTService(result={"data": [{"b64_json": ONE_PIXEL_PNG_B64}]}))

        with patch.dict(ai_module.require_permission.__globals__, {"auth_service": auth}), \
                patch.object(ai_module, "image_asset_service", assets):
            response = client.post(
                "/v1/images/generations",
                headers={"Authorization": f"Bearer {token}"},
                json={"prompt": "cat", "n": 1},
            )

        self.assertEqual(response.status_code, 200)
        archived = assets.list_assets(auth.authenticate(token), base_url="http://testserver")
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0]["width"], 1)
        self.assertEqual(archived[0]["height"], 1)

    def test_generation_refunds_quota_on_image_error(self):
        auth = self.create_auth()
        user, token, _ = auth.register_user("user@example.com", "StrongPass123")
        auth.adjust_user_quota(str(user["id"]), 1)
        client = self.create_client(auth, StubChatGPTService(error=ImageGenerationError("boom")))

        with patch.dict(ai_module.require_permission.__globals__, {"auth_service": auth}):
            response = client.post(
                "/v1/images/generations",
                headers={"Authorization": f"Bearer {token}"},
                json={"prompt": "cat", "n": 1},
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(auth.list_users()[0]["quota_balance"], 1)

    def test_generation_rejects_insufficient_quota(self):
        auth = self.create_auth()
        _, token, _ = auth.register_user("user@example.com", "StrongPass123")
        client = self.create_client(auth, StubChatGPTService())

        with patch.dict(ai_module.require_permission.__globals__, {"auth_service": auth}):
            response = client.post(
                "/v1/images/generations",
                headers={"Authorization": f"Bearer {token}"},
                json={"prompt": "cat", "n": 1},
            )

        self.assertEqual(response.status_code, 429)


if __name__ == "__main__":
    unittest.main()
