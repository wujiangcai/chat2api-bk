import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.auth_service import AuthService
from services.redemption_service import RedemptionService
from services.storage.json_storage import JSONStorageBackend

ROOT_DIR = Path(__file__).resolve().parents[1]
API_DIR = ROOT_DIR / "api"
api_package = types.ModuleType("api")
api_package.__path__ = [str(API_DIR)]
sys.modules.setdefault("api", api_package)

SUPPORT_SPEC = importlib.util.spec_from_file_location("api.support", API_DIR / "support.py")
support = importlib.util.module_from_spec(SUPPORT_SPEC)
assert SUPPORT_SPEC and SUPPORT_SPEC.loader
sys.modules["api.support"] = support
SUPPORT_SPEC.loader.exec_module(support)

ACCOUNTS_SPEC = importlib.util.spec_from_file_location("accounts_under_test", API_DIR / "accounts.py")
accounts = importlib.util.module_from_spec(ACCOUNTS_SPEC)
assert ACCOUNTS_SPEC and ACCOUNTS_SPEC.loader
ACCOUNTS_SPEC.loader.exec_module(accounts)

SYSTEM_SPEC = importlib.util.spec_from_file_location("system_admin_under_test", API_DIR / "system.py")
system = importlib.util.module_from_spec(SYSTEM_SPEC)
assert SYSTEM_SPEC and SYSTEM_SPEC.loader
SYSTEM_SPEC.loader.exec_module(system)


class AdminApiTests(unittest.TestCase):
    def create_client(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        base_dir = Path(tmp_dir.name)
        storage = JSONStorageBackend(base_dir / "accounts.json", base_dir / "auth_keys.json")
        auth = AuthService(storage)
        redemption = RedemptionService(storage)
        admin_key = auth.create_key(role="admin", name="Admin")[1]
        app = FastAPI()
        app.include_router(accounts.create_router())
        app.include_router(system.create_router("test"))
        return app, TestClient(app), auth, redemption, admin_key

    def patch_services(self, auth, redemption, admin_key):
        return patch.multiple(accounts, auth_service=auth, redemption_service=redemption), \
            patch.multiple(system, auth_service=auth, redemption_service=redemption), \
            patch.multiple(support, auth_service=auth), \
            patch("services.redemption_service.auth_service", auth)

    def test_admin_create_user_package_cdk_and_user_redeem(self):
        _, client, auth, redemption, admin_key = self.create_client()
        admin_headers = {"Authorization": f"Bearer {admin_key}"}
        patches = self.patch_services(auth, redemption, admin_key)
        with patches[0], patches[1], patches[2], patches[3]:
            user_response = client.post(
                "/api/admin/users",
                headers=admin_headers,
                json={"email": "user@example.com", "password": "StrongPass123", "quota_balance": 1},
            )
            self.assertEqual(user_response.status_code, 200)
            user_token = user_response.json()["token"]
            self.assertEqual(user_response.json()["item"]["quota_balance"], 1)

            package_response = client.post(
                "/api/admin/packages",
                headers=admin_headers,
                json={"name": "Pro", "quota": 10, "valid_days": 30},
            )
            self.assertEqual(package_response.status_code, 200)
            package_id = package_response.json()["item"]["id"]

            cdk_response = client.post(
                "/api/admin/cdks",
                headers=admin_headers,
                json={"name": "pkg", "type": "package", "package_id": package_id, "count": 1},
            )
            self.assertEqual(cdk_response.status_code, 200)
            code = cdk_response.json()["codes"][0]

            redeem_response = client.post(
                "/auth/redeem",
                headers={"Authorization": f"Bearer {user_token}"},
                json={"code": code},
            )
            self.assertEqual(redeem_response.status_code, 200)
            self.assertEqual(redeem_response.json()["user"]["quota_balance"], 11)
            self.assertEqual(redeem_response.json()["user"]["package_name"], "Pro")

            redemptions_response = client.get("/api/admin/redemptions", headers=admin_headers)
            self.assertEqual(redemptions_response.status_code, 200)
            self.assertEqual(len(redemptions_response.json()["items"]), 1)

            user_id = user_response.json()["item"]["id"]
            admin_ledger_response = client.get(f"/api/admin/quota-ledger?user_id={user_id}", headers=admin_headers)
            self.assertEqual(admin_ledger_response.status_code, 200)
            admin_ledger = admin_ledger_response.json()["items"]
            self.assertEqual([item["amount"] for item in reversed(admin_ledger)], [1, 10])
            self.assertEqual(admin_ledger[0]["reason"], "cdk")

            my_ledger_response = client.get("/api/me/quota-ledger", headers={"Authorization": f"Bearer {user_token}"})
            self.assertEqual(my_ledger_response.status_code, 200)
            self.assertEqual(len(my_ledger_response.json()["items"]), 2)


if __name__ == "__main__":
    unittest.main()
