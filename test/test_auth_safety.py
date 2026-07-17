import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

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

SYSTEM_PATH = API_DIR / "system.py"
SYSTEM_SPEC = importlib.util.spec_from_file_location("system_under_test", SYSTEM_PATH)
system = importlib.util.module_from_spec(SYSTEM_SPEC)
assert SYSTEM_SPEC and SYSTEM_SPEC.loader
SYSTEM_SPEC.loader.exec_module(system)


class AuthSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        system._login_attempts.clear()

    def test_login_rate_limit_blocks_after_configured_attempts(self):
        with patch.object(system, "LOGIN_RATE_LIMIT_PER_MINUTE", 2):
            system._check_login_rate_limit("USER@example.com")
            system._check_login_rate_limit("user@example.com")
            with self.assertRaises(HTTPException) as ctx:
                system._check_login_rate_limit("user@example.com")

        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(ctx.exception.headers["Retry-After"], "60")

    def test_register_rate_limit_blocks_ip_even_with_different_emails(self):
        with patch.object(system, "REGISTER_RATE_LIMIT_PER_MINUTE", 1):
            system._check_register_rate_limit("one@example.com")
            with self.assertRaises(HTTPException) as ctx:
                system._check_register_rate_limit("two@example.com")

        self.assertEqual(ctx.exception.status_code, 429)

    def test_redeem_rate_limit_blocks_anonymous_attempts(self):
        with patch.object(system, "REDEEM_RATE_LIMIT_PER_MINUTE", 1):
            system._check_redeem_rate_limit(None)
            with self.assertRaises(HTTPException) as ctx:
                system._check_redeem_rate_limit(None)

        self.assertEqual(ctx.exception.status_code, 429)

    def test_register_rate_limit_blocks_after_configured_attempts(self):
        with patch.object(system, "REGISTER_RATE_LIMIT_PER_MINUTE", 1):
            system._check_register_rate_limit("user@example.com")
            with self.assertRaises(HTTPException) as ctx:
                system._check_register_rate_limit("user@example.com")

        self.assertEqual(ctx.exception.status_code, 429)

    def test_key_login_rate_limit_blocks_after_configured_attempts(self):
        with patch.object(system, "KEY_LOGIN_RATE_LIMIT_PER_MINUTE", 1):
            system._check_key_login_rate_limit("Bearer sk-test")
            with self.assertRaises(HTTPException) as ctx:
                system._check_key_login_rate_limit("Bearer sk-test")

        self.assertEqual(ctx.exception.status_code, 429)

    def test_clear_login_attempts_resets_counter(self):
        with patch.object(system, "LOGIN_RATE_LIMIT_PER_MINUTE", 1):
            system._check_login_rate_limit("user@example.com")
            system._clear_login_attempts("user@example.com")
            system._check_login_rate_limit("user@example.com")

        self.assertEqual(len(system._login_attempts["login:unknown:user@example.com"]), 1)

    def test_register_route_rejects_when_disabled(self):
        router = system.create_router("test")
        route = next(route for route in router.routes if getattr(route, "path", "") == "/auth/register")
        register = route.endpoint

        with patch.object(system, "REGISTRATION_ENABLED", False):
            with self.assertRaises(HTTPException) as ctx:
                import asyncio
                asyncio.run(register(None, system.AuthRegisterRequest(email="user@example.com", password="StrongPass123")))

        self.assertEqual(ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
