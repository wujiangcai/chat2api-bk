from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.app import create_app
from services.config import config


class SecurityHeadersTests(unittest.TestCase):
    def test_security_headers_and_hsts_are_added(self):
        with patch.dict(os.environ, {"SECURITY_HEADERS_ENABLED": "true", "ENABLE_HSTS": "true", "HSTS_MAX_AGE_SECONDS": "123"}, clear=False):
            client = TestClient(create_app())
            response = client.get("/health/live")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertIn("default-src 'self'", response.headers["content-security-policy"])
        self.assertEqual(response.headers["strict-transport-security"], "max-age=123; includeSubDomains")

    def test_force_https_redirects_non_health_requests(self):
        with patch.dict(os.environ, {"FORCE_HTTPS": "true", "SECURITY_HEADERS_ENABLED": "true"}, clear=False):
            client = TestClient(create_app())
            response = client.get("/version", follow_redirects=False)
            health = client.get("/health/live", follow_redirects=False)

        self.assertEqual(response.status_code, 308)
        self.assertTrue(response.headers["location"].startswith("https://"))
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(health.status_code, 200)

    def test_cors_uses_configured_allowed_origins(self):
        with patch.dict(os.environ, {"WEB_ALLOWED_ORIGINS": "https://img.example.com"}, clear=False):
            client = TestClient(create_app())
            allowed = client.options(
                "/health/live",
                headers={"Origin": "https://img.example.com", "Access-Control-Request-Method": "GET"},
            )
            blocked = client.options(
                "/health/live",
                headers={"Origin": "https://evil.example.com", "Access-Control-Request-Method": "GET"},
            )

        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.headers.get("access-control-allow-origin"), "https://img.example.com")
        self.assertNotEqual(blocked.headers.get("access-control-allow-origin"), "https://evil.example.com")

    def test_production_without_allowed_origins_does_not_default_to_localhost(self):
        with patch.dict(os.environ, {"APP_ENV": "production"}, clear=False):
            with patch.dict(os.environ, {"WEB_ALLOWED_ORIGINS": ""}, clear=False):
                self.assertEqual(config.web_allowed_origins, [])


if __name__ == "__main__":
    unittest.main()
