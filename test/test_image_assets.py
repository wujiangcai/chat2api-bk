import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.assets as assets_api
from services.auth_service import AuthService
from services.image_asset_service import ImageAssetService
from services.storage.json_storage import JSONStorageBackend

ONE_PIXEL_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


class ImageAssetApiTests(unittest.TestCase):
    def create_services(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        base_dir = Path(tmp_dir.name)
        storage = JSONStorageBackend(base_dir / "accounts.json", base_dir / "auth_keys.json")
        auth = AuthService(storage)
        assets = ImageAssetService(storage, base_dir / "assets")
        app = FastAPI()
        app.include_router(assets_api.create_router())
        return TestClient(app), auth, assets

    def patch_services(self, auth: AuthService, assets: ImageAssetService):
        return patch.multiple(assets_api, image_asset_service=assets), patch.dict(
            assets_api.require_identity.__globals__,
            {"auth_service": auth},
        )

    def test_user_lists_gets_and_deletes_own_asset(self):
        client, auth, assets = self.create_services()
        user, token, _ = auth.register_user("user@example.com", "StrongPass123")
        identity = auth.authenticate(token)
        created = assets.archive_result(
            owner=identity,
            result={"data": [{"b64_json": ONE_PIXEL_PNG_B64, "revised_prompt": "one pixel"}]},
            job_id="job_1",
            source="unit-test",
            model="gpt-image-2",
            prompt="tiny image",
            base_url="http://testserver",
        )
        asset_id = created[0]["id"]

        patches = self.patch_services(auth, assets)
        with patches[0], patches[1]:
            list_response = client.get("/api/assets", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(list_response.status_code, 200)
            self.assertEqual(len(list_response.json()["items"]), 1)
            self.assertEqual(list_response.json()["items"][0]["width"], 1)
            self.assertEqual(list_response.json()["items"][0]["height"], 1)

            get_response = client.get(f"/api/assets/{asset_id}", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(get_response.status_code, 200)
            self.assertEqual(get_response.json()["asset"]["job_id"], "job_1")

            delete_response = client.delete(f"/api/assets/{asset_id}", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(delete_response.status_code, 200)
            self.assertEqual(delete_response.json()["asset"]["status"], "deleted")

            list_after_delete = client.get("/api/assets", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(list_after_delete.status_code, 200)
            self.assertEqual(list_after_delete.json()["items"], [])

        self.assertEqual(assets.get_asset(str(asset_id))["status"], "deleted")

    def test_asset_access_is_owner_scoped_and_admin_can_list_all(self):
        client, auth, assets = self.create_services()
        _, owner_token, _ = auth.register_user("owner@example.com", "StrongPass123")
        _, other_token, _ = auth.register_user("other@example.com", "StrongPass123")
        admin_key = auth.create_key(role="admin", name="Admin")[1]
        owner_identity = auth.authenticate(owner_token)
        created = assets.archive_result(
            owner=owner_identity,
            result={"data": [{"b64_json": ONE_PIXEL_PNG_B64}]},
            job_id="job_2",
            source="unit-test",
            model="gpt-image-2",
            prompt="tiny image",
            base_url="http://testserver",
        )
        asset_id = created[0]["id"]

        patches = self.patch_services(auth, assets)
        with patches[0], patches[1]:
            other_get = client.get(f"/api/assets/{asset_id}", headers={"Authorization": f"Bearer {other_token}"})
            self.assertEqual(other_get.status_code, 404)

            admin_list = client.get("/api/admin/assets", headers={"Authorization": f"Bearer {admin_key}"})
            self.assertEqual(admin_list.status_code, 200)
            self.assertEqual(len(admin_list.json()["items"]), 1)
            self.assertEqual(admin_list.json()["items"][0]["owner"]["email"], "owner@example.com")


if __name__ == "__main__":
    unittest.main()
