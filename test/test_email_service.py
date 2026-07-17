from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import support, system
from services.auth_service import AuthService
from services.email_service import EmailDeliveryResult, EmailService
from services.storage.json_storage import JSONStorageBackend


class EmailServiceTests(unittest.TestCase):
    def test_builds_public_action_urls(self):
        service = EmailService(env={"EMAIL_PROVIDER": "console", "APP_PUBLIC_URL": "https://img.example.com/"})

        self.assertEqual(
            service.action_url("/verify-email", "ev-token+/="),
            "https://img.example.com/verify-email?token=ev-token%2B%2F%3D",
        )
        with redirect_stderr(StringIO()):
            result = service.send_email_verification(to="user@example.com", token="ev-token", expires_at="2030-01-01T00:00:00Z")
        self.assertTrue(result.sent)
        self.assertEqual(result.provider, "console")

    def test_smtp_provider_sends_message(self):
        calls: list[tuple[str, object]] = []

        class FakeSMTP:
            def __init__(self, host, port, timeout=10):
                calls.append(("connect", (host, port, timeout)))

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return None

            def starttls(self, context=None):
                calls.append(("starttls", bool(context)))

            def login(self, username, password):
                calls.append(("login", (username, password)))

            def send_message(self, message):
                calls.append(("send_message", (message["To"], message["Subject"], message["From"])))

        env = {
            "EMAIL_PROVIDER": "smtp",
            "SMTP_HOST": "smtp.example.com",
            "SMTP_PORT": "587",
            "SMTP_USERNAME": "apikey",
            "SMTP_PASSWORD": "secret",
            "EMAIL_FROM": "ChatGPT2API <no-reply@example.com>",
        }
        service = EmailService(env=env)

        with patch("services.email_service.smtplib.SMTP", FakeSMTP):
            result = service.send_email(to="user@example.com", subject="Hello", text="Body")

        self.assertTrue(result.sent)
        self.assertEqual(result.provider, "smtp")
        self.assertIn(("connect", ("smtp.example.com", 587, 10)), calls)
        self.assertIn(("starttls", True), calls)
        self.assertIn(("login", ("apikey", "secret")), calls)
        self.assertIn(("send_message", ("user@example.com", "Hello", "ChatGPT2API <no-reply@example.com>")), calls)

    def test_resend_provider_posts_json_payload(self):
        captured: dict[str, object] = {}

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return None

            def read(self, *_):
                return b"{}"

        def fake_urlopen(request, timeout=10):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            captured["headers"] = dict(request.header_items())
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse()

        service = EmailService(
            env={
                "EMAIL_PROVIDER": "resend",
                "RESEND_API_KEY": "rk_test",
                "EMAIL_FROM": "no-reply@example.com",
                "RESEND_API_URL": "https://resend.example.test/emails",
            }
        )

        with patch("services.email_service.urllib.request.urlopen", fake_urlopen):
            result = service.send_email(to="user@example.com", subject="Hello", text="Body")

        self.assertTrue(result.sent)
        self.assertEqual(captured["url"], "https://resend.example.test/emails")
        self.assertEqual(captured["payload"]["to"], ["user@example.com"])
        self.assertEqual(captured["payload"]["subject"], "Hello")
        self.assertIn("Bearer rk_test", captured["headers"].get("Authorization", ""))


class EmailAuthApiTests(unittest.TestCase):
    def create_auth_service(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        base_dir = Path(tmp_dir.name)
        storage = JSONStorageBackend(base_dir / "accounts.json", base_dir / "auth_keys.json")
        return AuthService(storage)

    def test_registration_sends_verification_email_when_required(self):
        auth = self.create_auth_service()
        calls: list[dict[str, object]] = []

        class FakeEmailService:
            provider = "smtp"

            def status(self):
                return {"provider": "smtp", "configured": True}

            def send_email_verification(self, **kwargs):
                calls.append(dict(kwargs))
                return EmailDeliveryResult(True, "smtp", "sent")

        app = FastAPI()
        app.include_router(system.create_router("test"))
        client = TestClient(app)

        with (
            patch.object(system, "auth_service", auth),
            patch.object(support, "auth_service", auth),
            patch.dict(system.require_identity.__globals__, {"auth_service": auth}),
            patch.object(system, "email_service", FakeEmailService()),
            patch.object(system, "REGISTRATION_ENABLED", True),
            patch.object(system, "EMAIL_VERIFICATION_REQUIRED", True),
            patch.dict("os.environ", {"AUTH_RETURN_ACTION_TOKENS": "true", "AUTH_RESPONSE_INCLUDE_TOKEN": "true"}),
        ):
            response = client.post(
                "/auth/register",
                json={"email": "user@example.com", "password": "StrongPass123", "name": "User"},
            )

        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["verification_required"])
        self.assertTrue(payload["email_sent"])
        self.assertEqual(payload["email_provider"], "smtp")
        self.assertIn("verification_token", payload)
        self.assertNotIn("token", payload)
        self.assertEqual(calls[0]["to"], "user@example.com")
        self.assertTrue(str(calls[0]["token"]).startswith("ev-"))


if __name__ == "__main__":
    unittest.main()
