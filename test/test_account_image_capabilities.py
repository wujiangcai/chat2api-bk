from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")

from services.account_service import AccountService
from services.auth_service import AuthService
from services.storage.json_storage import JSONStorageBackend
from utils.helper import anonymize_token


class AccountCapabilityTests(unittest.TestCase):
    def test_unknown_quota_accounts_are_available_only_when_not_throttled(self) -> None:
        self.assertFalse(
            AccountService._is_image_account_available(
                {"status": "限流", "image_quota_unknown": True, "quota": 0}
            )
        )
        self.assertTrue(
            AccountService._is_image_account_available(
                {"status": "正常", "image_quota_unknown": True, "quota": 0}
            )
        )

    def test_paid_accounts_remain_available_when_cached_quota_is_zero(self) -> None:
        self.assertTrue(
            AccountService._is_image_account_available(
                {"status": "正常", "type": "Plus", "image_quota_unknown": False, "quota": 0}
            )
        )
        self.assertFalse(
            AccountService._is_image_account_available(
                {"status": "正常", "type": "Free", "image_quota_unknown": False, "quota": 0}
            )
        )

    def test_free_account_availability_states(self) -> None:
        # Free 账号在 image_quota_unknown == True 时为可用
        self.assertTrue(
            AccountService._is_image_account_available(
                {"status": "正常", "type": "Free", "image_quota_unknown": True, "quota": 0}
            )
        )
        # Free 账号在 image_quota_unknown == False 且 quota == 0 时不可用
        self.assertFalse(
            AccountService._is_image_account_available(
                {"status": "正常", "type": "Free", "image_quota_unknown": False, "quota": 0}
            )
        )
        # Free 账号在 quota > 0 时可用
        self.assertTrue(
            AccountService._is_image_account_available(
                {"status": "正常", "type": "Free", "image_quota_unknown": False, "quota": 3}
            )
        )

    def test_throttled_account_with_expired_restore_at_is_available(self) -> None:
        past_iso = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        self.assertTrue(
            AccountService._is_image_account_available(
                {"status": "限流", "type": "Free", "restore_at": past_iso, "quota": 0}
            )
        )
        self.assertTrue(
            AccountService._is_image_account_available(
                {"status": "限流", "type": "Free", "restoreAt": "2020-01-01T00:00:00Z", "quota": 0}
            )
        )
        future_iso = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        self.assertFalse(
            AccountService._is_image_account_available(
                {"status": "限流", "type": "Free", "restore_at": future_iso, "quota": 0}
            )
        )
        self.assertFalse(
            AccountService._is_image_account_available(
                {"status": "限流", "type": "Free", "restore_at": None, "quota": 0}
            )
        )
        self.assertFalse(
            AccountService._is_image_account_available(
                {"status": "限流", "type": "Free", "restore_at": "invalid-date", "quota": 0}
            )
        )

    @patch("services.account_service.Session")
    def test_fetch_remote_info_free_account_status(self, mock_session_cls: MagicMock) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))

            mock_session = MagicMock()
            mock_session_cls.return_value = mock_session
            mock_session.headers = {}

            me_resp = MagicMock()
            me_resp.status_code = 200
            me_resp.json.return_value = {
                "id": "user-123",
                "email": "free_user@example.com",
            }

            # 1. init payload 未下发 limits_progress (image_quota_unknown == True)
            init_resp_unknown = MagicMock()
            init_resp_unknown.status_code = 200
            init_resp_unknown.json.return_value = {
                "default_model_slug": "gpt-4o",
                "limits_progress": [],
            }

            mock_session.get.return_value = me_resp
            mock_session.post.return_value = init_resp_unknown

            info = service.fetch_remote_info("token-free-1")
            self.assertEqual(info["type"], "Free")
            self.assertTrue(info["image_quota_unknown"])
            self.assertEqual(info["quota"], 0)
            self.assertEqual(info["status"], "正常")

            # 2. init payload 下发 image_gen 且 remaining == 0 (image_quota_unknown == False)
            init_resp_zero = MagicMock()
            init_resp_zero.status_code = 200
            init_resp_zero.json.return_value = {
                "default_model_slug": "gpt-4o",
                "limits_progress": [
                    {
                        "feature_name": "image_gen",
                        "remaining": 0,
                        "reset_after": "2026-09-01T12:00:00Z",
                    }
                ],
            }
            mock_session.post.return_value = init_resp_zero

            info_zero = service.fetch_remote_info("token-free-2")
            self.assertEqual(info_zero["type"], "Free")
            self.assertFalse(info_zero["image_quota_unknown"])
            self.assertEqual(info_zero["quota"], 0)
            self.assertEqual(info_zero["status"], "限流")
            self.assertEqual(info_zero["restore_at"], "2026-09-01T12:00:00Z")

            # 3. init payload 下发 image_gen 且 remaining == 3 (image_quota_unknown == False)
            init_resp_positive = MagicMock()
            init_resp_positive.status_code = 200
            init_resp_positive.json.return_value = {
                "default_model_slug": "gpt-4o",
                "limits_progress": [
                    {
                        "feature_name": "image_gen",
                        "remaining": 3,
                        "reset_after": "2026-09-01T12:00:00Z",
                    }
                ],
            }
            mock_session.post.return_value = init_resp_positive

            info_pos = service.fetch_remote_info("token-free-3")
            self.assertEqual(info_pos["type"], "Free")
            self.assertFalse(info_pos["image_quota_unknown"])
            self.assertEqual(info_pos["quota"], 3)
            self.assertEqual(info_pos["status"], "正常")

    def test_prolite_variants_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            self.assertEqual(service._normalize_account_type("prolite"), "ProLite")
            self.assertEqual(service._normalize_account_type("pro_lite"), "ProLite")

    def test_search_account_type_ignores_unrelated_scalar_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            self.assertIsNone(
                service._search_account_type(
                    {
                        "amr": ["pwd", "otp", "mfa"],
                        "chatgpt_compute_residency": "no_constraint",
                        "chatgpt_data_residency": "no_constraint",
                        "user_id": "user-I52GFfLGFM0dokFk2dBiKEBn",
                    }
                )
            )

    def test_mark_image_result_does_not_consume_unknown_quota(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_accounts(["token-1"])
            service.update_account(
                "token-1",
                {
                    "status": "正常",
                    "quota": 0,
                    "image_quota_unknown": True,
                },
            )

            updated = service.mark_image_result("token-1", success=True)

            self.assertIsNotNone(updated)
            self.assertEqual(updated["quota"], 0)
            self.assertEqual(updated["status"], "正常")
            self.assertTrue(updated["image_quota_unknown"])

    def test_disabled_account_is_not_available(self) -> None:
        self.assertFalse(
            AccountService._is_image_account_available(
                {"status": "正常", "type": "Free", "quota": 20, "disabled": True}
            )
        )

    def test_auto_disable_keeps_last_available_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_accounts(["token-keep", "token-drop"])
            for token in ("token-keep", "token-drop"):
                service.update_account(token, {"status": "正常", "quota": 5, "disabled": False})

            with patch("services.account_service.config") as mock_config:
                mock_config.auto_disable_consecutive_fail = 1
                first = service.mark_image_result("token-drop", success=False)
                second = service.mark_image_result("token-keep", success=False)

            self.assertTrue(first["disabled"])
            self.assertTrue(first["auto_disabled"])
            self.assertFalse(second["disabled"])
            self.assertFalse(second["auto_disabled"])
            self.assertEqual(second["consecutive_fail"], 1)
            self.assertTrue(
                AccountService._is_image_account_available(service.get_account("token-keep") or {})
            )

    def test_enabling_account_clears_consecutive_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AccountService(JSONStorageBackend(Path(tmp_dir) / "accounts.json"))
            service.add_accounts(["token-1"])
            service.update_account("token-1", {"disabled": True, "consecutive_fail": 5, "quota": 9})
            updated = service.update_account("token-1", {"disabled": False})
            self.assertFalse(updated["disabled"])
            self.assertEqual(updated["consecutive_fail"], 0)


class TokenLogTests(unittest.TestCase):
    def test_anonymize_token_hides_raw_value(self) -> None:
        token = "super-secret-token"
        token_ref = anonymize_token(token)

        self.assertTrue(token_ref.startswith("token:"))
        self.assertNotIn(token, token_ref)


class AuthServiceTests(unittest.TestCase):
    def test_create_authenticate_disable_and_delete_user_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AuthService(JSONStorageBackend(Path(tmp_dir) / "accounts.json", Path(tmp_dir) / "auth_keys.json"))

            item, raw_key = service.create_key(role="user", name="Alice")

            self.assertEqual(item["role"], "user")
            self.assertEqual(item["name"], "Alice")
            self.assertTrue(item["enabled"])
            self.assertTrue(raw_key.startswith("sk-"))

            authed = service.authenticate(raw_key)
            self.assertIsNotNone(authed)
            self.assertEqual(authed["id"], item["id"])
            self.assertEqual(authed["role"], "user")
            self.assertIsNotNone(authed["last_used_at"])

            updated = service.update_key(item["id"], {"enabled": False}, role="user")
            self.assertIsNotNone(updated)
            self.assertFalse(updated["enabled"])
            self.assertIsNone(service.authenticate(raw_key))

            self.assertTrue(service.delete_key(item["id"], role="user"))
            self.assertFalse(service.delete_key(item["id"], role="user"))
            self.assertEqual(service.list_keys(role="user"), [])

    def test_authenticate_ignores_last_used_save_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = AuthService(JSONStorageBackend(Path(tmp_dir) / "accounts.json", Path(tmp_dir) / "auth_keys.json"))
            item, raw_key = service.create_key(role="user", name="Alice")

            def fail_save() -> None:
                raise OSError("disk unavailable")

            service._save = fail_save

            authed = service.authenticate(raw_key)

            self.assertIsNotNone(authed)
            self.assertEqual(authed["id"], item["id"])
            self.assertIsNotNone(authed["last_used_at"])


if __name__ == "__main__":
    unittest.main()
