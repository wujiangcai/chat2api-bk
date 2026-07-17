import tempfile
import unittest
from pathlib import Path

from services.auth_service import AuthService, DEFAULT_USER_PERMISSIONS, _hash_key
from services.storage.json_storage import JSONStorageBackend


class AuthServicePermissionTests(unittest.TestCase):
    def create_service(self, initial_items=None):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        base_dir = Path(tmp_dir.name)
        storage = JSONStorageBackend(base_dir / "accounts.json", base_dir / "auth_keys.json")
        if initial_items is not None:
            storage.save_auth_keys(initial_items)
        return AuthService(storage)

    def test_legacy_user_key_gets_default_commercial_fields(self):
        service = self.create_service([
            {
                "id": "legacy-user",
                "name": "Legacy",
                "role": "user",
                "key_hash": _hash_key("sk-legacy"),
                "enabled": True,
            }
        ])

        identity = service.authenticate("sk-legacy")

        self.assertIsNotNone(identity)
        assert identity is not None
        self.assertEqual(identity["permissions"], DEFAULT_USER_PERMISSIONS)
        self.assertIsNone(identity["quota_limit"])
        self.assertEqual(identity["quota_used"], 0)
        self.assertIsNone(identity["quota_remaining"])
        self.assertIsNone(identity["rate_limit_per_minute"])

    def test_permission_and_quota_flow(self):
        service = self.create_service()
        item, raw_key = service.create_key(
            role="user",
            name="Paid user",
            permissions=["image.generate"],
            quota_limit=2,
            rate_limit_per_minute=3,
        )

        identity = service.authenticate(raw_key)

        self.assertIsNotNone(identity)
        assert identity is not None
        self.assertTrue(service.has_permission(identity, "image.generate"))
        self.assertFalse(service.has_permission(identity, "image.edit"))
        self.assertTrue(service.check_quota(identity, 2))
        self.assertFalse(service.check_quota(identity, 3))

        updated = service.consume_quota(str(item["id"]), 1)

        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated["quota_used"], 1)
        self.assertEqual(updated["quota_remaining"], 1)

    def test_admin_has_wildcard_permission_and_no_quota_limit(self):
        service = self.create_service()
        _, raw_key = service.create_key(role="admin", name="Admin", quota_limit=1)

        identity = service.authenticate(raw_key)

        self.assertIsNotNone(identity)
        assert identity is not None
        self.assertTrue(service.has_permission(identity, "anything"))
        self.assertTrue(service.check_quota(identity, 99))

    def test_expired_key_is_detected(self):
        service = self.create_service()
        _, raw_key = service.create_key(role="user", name="Expired", expires_at="2000-01-01T00:00:00+00:00")

        identity = service.authenticate(raw_key)

        self.assertIsNotNone(identity)
        assert identity is not None
        self.assertTrue(service.is_expired(identity))

    def test_update_can_reset_and_make_quota_unlimited(self):
        service = self.create_service()
        item, _ = service.create_key(role="user", name="Quota", quota_limit=3)
        service.consume_quota(str(item["id"]), 2)

        updated = service.update_key(str(item["id"]), {"quota_limit": None, "reset_quota_used": True}, role="user")

        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertIsNone(updated["quota_limit"])
        self.assertEqual(updated["quota_used"], 0)
        self.assertIsNone(updated["quota_remaining"])
    def test_register_rejects_weak_password(self):
        service = self.create_service()

        with self.assertRaises(ValueError):
            service.register_user("user@example.com", "password123")
        with self.assertRaises(ValueError):
            service.register_user("user@example.com", "user")
        with self.assertRaises(ValueError):
            service.register_user("user@example.com", "11111111")
        with self.assertRaises(ValueError):
            service.register_user("user@example.com", "a       ")


if __name__ == "__main__":
    unittest.main()
