import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.auth_service import AuthService
from services.redemption_service import RedemptionService
from services.storage.database_storage import DatabaseStorageBackend
from services.storage.json_storage import JSONStorageBackend


class EmailPasswordAuthTests(unittest.TestCase):
    def create_service(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        base_dir = Path(tmp_dir.name)
        storage = JSONStorageBackend(base_dir / "accounts.json", base_dir / "auth_keys.json")
        return AuthService(storage)

    def test_register_and_login_rotate_token(self):
        service = self.create_service()

        user, first_token, first_key = service.register_user("USER@example.com", "StrongPass123", "User")
        identity = service.authenticate(first_token)

        self.assertEqual(user["email"], "user@example.com")
        self.assertEqual(identity["user_id"], user["id"])
        self.assertEqual(identity["email"], "user@example.com")
        self.assertEqual(first_key["kind"], "login")

        _, second_token, second_key = service.login_user("user@example.com", "StrongPass123")

        self.assertNotEqual(first_token, second_token)
        self.assertEqual(first_key["id"], second_key["id"])
        self.assertIsNone(service.authenticate(first_token))
        self.assertEqual(service.authenticate(second_token)["user_id"], user["id"])

    def test_duplicate_email_and_wrong_password_fail(self):
        service = self.create_service()
        service.register_user("user@example.com", "StrongPass123")

        with self.assertRaises(ValueError):
            service.register_user("USER@example.com", "StrongPass123")
        with self.assertRaises(ValueError):
            service.login_user("user@example.com", "wrong-password")

    def test_user_quota_consume_and_refund(self):
        service = self.create_service()
        user, _, _ = service.register_user("user@example.com", "StrongPass123")
        service.adjust_user_quota(str(user["id"]), 2, "admin-test", ref_type="unit-test", ref_id="grant-1")

        self.assertTrue(service.try_consume_user_quota(str(user["id"]), 2, ref_type="job", ref_id="job-1"))
        self.assertFalse(service.try_consume_user_quota(str(user["id"]), 1))
        service.refund_user_quota(str(user["id"]), 1, ref_type="job", ref_id="job-1")
        self.assertTrue(service.try_consume_user_quota(str(user["id"]), 1, ref_type="job", ref_id="job-2"))

        ledger = list(reversed(service.list_quota_ledger(str(user["id"]))))
        self.assertEqual([item["type"] for item in ledger], ["grant", "consume", "refund", "consume"])
        self.assertEqual([item["amount"] for item in ledger], [2, -2, 1, -1])
        self.assertEqual([item["balance_after"] for item in ledger], [2, 0, 1, 0])
        self.assertEqual(ledger[1]["ref_id"], "job-1")

    def test_quota_ledger_is_persisted(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        base_dir = Path(tmp_dir.name)
        storage = JSONStorageBackend(base_dir / "accounts.json", base_dir / "auth_keys.json")
        service = AuthService(storage)
        user, _, _ = service.register_user("user@example.com", "StrongPass123")
        service.adjust_user_quota(str(user["id"]), 3, "seed")

        reloaded = AuthService(storage)
        ledger = reloaded.list_quota_ledger(str(user["id"]))

        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["amount"], 3)
        self.assertEqual(ledger[0]["balance_after"], 3)

    def test_quota_ledger_uses_database_table(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        db_path = Path(tmp_dir.name) / "commercial.db"
        storage = DatabaseStorageBackend(f"sqlite:///{db_path.as_posix()}")
        self.addCleanup(storage.engine.dispose)
        service = AuthService(storage)
        user, _, _ = service.register_user("user@example.com", "StrongPass123")

        service.adjust_user_quota(str(user["id"]), 3, "seed", ref_type="unit-test", ref_id="grant-1")
        self.assertTrue(service.try_consume_user_quota(str(user["id"]), 1, ref_type="job", ref_id="job-1"))

        health = storage.health_check()
        self.assertEqual(health["status"], "healthy")
        self.assertEqual(health["quota_ledger_count"], 2)
        self.assertEqual(health["collection_item_count"], 0)
        self.assertEqual(health["dedicated_collection_counts"]["users"], 1)

        reloaded_storage = DatabaseStorageBackend(f"sqlite:///{db_path.as_posix()}")
        self.addCleanup(reloaded_storage.engine.dispose)
        reloaded = AuthService(reloaded_storage)
        ledger = list(reversed(reloaded.list_quota_ledger(str(user["id"]))))
        self.assertEqual([item["amount"] for item in ledger], [3, -1])
        self.assertEqual(ledger[0]["ref_id"], "grant-1")
        self.assertEqual(ledger[1]["ref_id"], "job-1")


class RedemptionServiceTests(unittest.TestCase):
    def create_services(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        base_dir = Path(tmp_dir.name)
        storage = JSONStorageBackend(base_dir / "accounts.json", base_dir / "auth_keys.json")
        auth = AuthService(storage)
        redemption = RedemptionService(storage)
        return auth, redemption

    def test_quota_cdk_redeem_adds_balance_once(self):
        auth, redemption = self.create_services()
        user, token, _ = auth.register_user("user@example.com", "StrongPass123")
        identity = auth.authenticate(token)
        created = redemption.create_cdks(name="quota", type="quota", quota=5, count=1)
        code = created["codes"][0]

        with patch("services.redemption_service.auth_service", auth):
            result = redemption.redeem(code, identity)
            self.assertEqual(result["user"]["quota_balance"], 5)
            with self.assertRaises(ValueError):
                redemption.redeem(code, identity)

        ledger = auth.list_quota_ledger(str(user["id"]))
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["type"], "grant")
        self.assertEqual(ledger[0]["reason"], "cdk")
        self.assertEqual(ledger[0]["ref_type"], "cdk")
        self.assertEqual(ledger[0]["amount"], 5)

    def test_package_cdk_applies_package_and_quota(self):
        auth, redemption = self.create_services()
        _, token, _ = auth.register_user("user@example.com", "StrongPass123")
        identity = auth.authenticate(token)
        package = redemption.create_package(name="Pro", quota=10, valid_days=30)
        created = redemption.create_cdks(name="pkg", type="package", package_id=str(package["id"]), count=1)

        with patch("services.redemption_service.auth_service", auth):
            result = redemption.redeem(created["codes"][0], identity)

        self.assertEqual(result["user"]["quota_balance"], 10)
        self.assertEqual(result["user"]["package_name"], "Pro")
        self.assertIsNotNone(result["user"]["package_expires_at"])


if __name__ == "__main__":
    unittest.main()
