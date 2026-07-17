import importlib.util
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException

SUPPORT_PATH = Path(__file__).resolve().parents[1] / "api" / "support.py"
SUPPORT_SPEC = importlib.util.spec_from_file_location("support_under_test", SUPPORT_PATH)
support = importlib.util.module_from_spec(SUPPORT_SPEC)
assert SUPPORT_SPEC and SUPPORT_SPEC.loader
SUPPORT_SPEC.loader.exec_module(support)


class SupportPermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        support._recent_usage.clear()

    def test_require_permission_returns_identity_when_allowed(self):
        fake_auth = Mock()
        fake_auth.authenticate.return_value = {"id": "user-1", "role": "user", "permissions": ["image.generate"]}
        fake_auth.is_expired.return_value = False
        fake_auth.has_permission.return_value = True

        with patch.object(support, "auth_service", fake_auth):
            identity = support.require_permission("Bearer sk-test", "image.generate")

        self.assertEqual(identity["id"], "user-1")
        fake_auth.has_permission.assert_called_once_with(identity, "image.generate")

    def test_require_permission_rejects_missing_permission(self):
        fake_auth = Mock()
        fake_auth.authenticate.return_value = {"id": "user-1", "role": "user", "permissions": []}
        fake_auth.is_expired.return_value = False
        fake_auth.has_permission.return_value = False

        with patch.object(support, "auth_service", fake_auth):
            with self.assertRaises(HTTPException) as ctx:
                support.require_permission("Bearer sk-test", "image.edit")

        self.assertEqual(ctx.exception.status_code, 403)

    def test_check_quota_rejects_insufficient_quota(self):
        fake_auth = Mock()
        fake_auth.check_quota.return_value = False
        identity = {"id": "user-1", "quota_limit": 1, "quota_used": 1, "quota_remaining": 0}

        with patch.object(support, "auth_service", fake_auth):
            with self.assertRaises(HTTPException) as ctx:
                support.check_quota(identity, 1)

        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(ctx.exception.detail["quota_remaining"], 0)

    def test_check_rate_limit_blocks_over_limit(self):
        identity = {"id": "user-1", "role": "user", "rate_limit_per_minute": 2}

        support.check_rate_limit(identity, 2)
        with self.assertRaises(HTTPException) as ctx:
            support.check_rate_limit(identity, 1)

        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(ctx.exception.headers["Retry-After"], "60")

    def test_count_success_items_counts_only_successful_images(self):
        result = {
            "data": [
                {"url": "https://example.com/1.png"},
                {"b64_json": "abc"},
                {"error": "failed", "url": "https://example.com/failed.png"},
                {},
            ]
        }

        self.assertEqual(support.count_success_items(result, 4), 2)


if __name__ == "__main__":
    unittest.main()
