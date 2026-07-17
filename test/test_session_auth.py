from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import support, system
from services.auth_service import AuthService
from services.storage.json_storage import JSONStorageBackend


class SessionAndAccountRecoveryTests(unittest.TestCase):
    def create_auth_service(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        base_dir = Path(tmp_dir.name)
        storage = JSONStorageBackend(base_dir / "accounts.json", base_dir / "auth_keys.json")
        return AuthService(storage)

    def test_http_only_session_token_authenticates_and_can_be_revoked(self):
        auth = self.create_auth_service()
        user, raw_key, key = auth.register_user("user@example.com", "StrongPass123")
        identity = {**key, **user, "user_id": user["id"], "key_id": key["id"]}

        session, session_token = auth.create_session(identity, ttl_seconds=600)
        session_identity = auth.authenticate(session_token)

        self.assertEqual(session["user_id"], user["id"])
        self.assertEqual(session_identity["user_id"], user["id"])
        self.assertEqual(session_identity["email"], "user@example.com")
        self.assertTrue(auth.revoke_session(session_token))
        self.assertIsNone(auth.authenticate(session_token))
        self.assertIsNotNone(auth.authenticate(raw_key))

    def test_email_verification_and_password_reset_tokens_are_single_use(self):
        auth = self.create_auth_service()
        user, old_token, key = auth.register_user("user@example.com", "StrongPass123")
        session_identity = {**key, **user, "user_id": user["id"], "key_id": key["id"]}
        _, session_token = auth.create_session(session_identity, ttl_seconds=600)

        _, verify_token = auth.create_email_verification_token(str(user["id"]))
        verified = auth.verify_email_token(verify_token)
        self.assertTrue(verified["email_verified"])
        with self.assertRaisesRegex(ValueError, "already used"):
            auth.verify_email_token(verify_token)

        _, reset_token = auth.create_password_reset_token("USER@example.com")
        reset_user = auth.reset_password_with_token(str(reset_token), "NewStrongPass123")
        self.assertEqual(reset_user["email"], "user@example.com")
        self.assertIsNone(auth.authenticate(old_token))
        self.assertIsNone(auth.authenticate(session_token))
        _, new_token, _ = auth.login_user("user@example.com", "NewStrongPass123")
        self.assertEqual(auth.authenticate(new_token)["user_id"], user["id"])

    def test_system_login_sets_cookie_and_cookie_auth_me_works(self):
        auth = self.create_auth_service()
        auth.register_user("user@example.com", "StrongPass123")
        app = FastAPI()
        app.include_router(system.create_router("test"))
        client = TestClient(app)

        with (
            patch.object(system, "auth_service", auth),
            patch.object(support, "auth_service", auth),
            patch.dict(system.require_identity.__globals__, {"auth_service": auth}),
            patch.object(system, "REGISTRATION_ENABLED", True),
            patch.dict("os.environ", {"AUTH_RESPONSE_INCLUDE_TOKEN": "false", "AUTH_SESSION_COOKIE_SECURE": "false", "AUTH_SESSION_COOKIE_SAMESITE": "lax"}),
        ):
            login_response = client.post("/auth/login", json={"email": "user@example.com", "password": "StrongPass123"})
            self.assertEqual(login_response.status_code, 200)
            self.assertNotIn("token", login_response.json())
            self.assertTrue(login_response.json()["session_cookie"])
            self.assertIn(system.AUTH_SESSION_COOKIE_NAME, login_response.cookies)
            session_cookie = login_response.cookies.get(system.AUTH_SESSION_COOKIE_NAME)
            self.assertTrue(session_cookie)
            client.cookies.set(system.AUTH_SESSION_COOKIE_NAME, session_cookie)

            me_response = client.get("/auth/me")
            self.assertEqual(me_response.status_code, 200)
            self.assertEqual(me_response.json()["email"], "user@example.com")

            logout_response = client.post("/auth/logout")
            self.assertEqual(logout_response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
