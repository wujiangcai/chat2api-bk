from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api import support, system
from services.auth_service import AuthService
from services.production_readiness import ProductionReadinessService
from services.storage.json_storage import JSONStorageBackend
from services.storage.migrations.versions import ALL_MIGRATIONS


class FakeConfig:
    def __init__(
        self,
        *,
        is_production: bool,
        base_url: str,
        origins: list[str],
        security_headers_enabled: bool = True,
        hsts_enabled: bool = True,
        auth_key: str = "prod-auth-key",
    ):
        self.is_production = is_production
        self.base_url = base_url
        self.web_allowed_origins = origins
        self.security_headers_enabled = security_headers_enabled
        self.hsts_enabled = hsts_enabled
        self.auth_key = auth_key


class FakeStorage:
    def __init__(self, *, info: dict[str, object], health: dict[str, object]):
        self.info = info
        self.health = health

    def get_backend_info(self):
        return dict(self.info)

    def health_check(self):
        return dict(self.health)


class FakeObjectStorage:
    def __init__(self, info: dict[str, object]):
        self._info = info

    def info(self):
        return dict(self._info)


class FakeJobService:
    def __init__(self, info: dict[str, object]):
        self._info = info

    def queue_info(self):
        return dict(self._info)


class ProductionReadinessTests(unittest.TestCase):
    def create_service(
        self,
        *,
        config: FakeConfig,
        storage: FakeStorage,
        object_storage: FakeObjectStorage,
        job_service: FakeJobService,
        env: dict[str, str],
    ) -> ProductionReadinessService:
        return ProductionReadinessService(
            config_obj=config,
            storage_factory=lambda: storage,
            object_storage_factory=lambda: object_storage,
            image_job_service_factory=lambda: job_service,
            env=env,
        )

    def test_passes_for_production_postgres_redis_and_remote_object_storage(self):
        service = self.create_service(
            config=FakeConfig(
                is_production=True,
                base_url="https://img.example.com",
                origins=["https://img.example.com"],
            ),
            storage=FakeStorage(
                info={"type": "database", "db_type": "postgresql"},
                health={"status": "healthy", "schema_migration_count": len(ALL_MIGRATIONS)},
            ),
            object_storage=FakeObjectStorage(
                {"backend": "s3", "public_base_url": "https://cdn.example.com"}
            ),
            job_service=FakeJobService(
                {"backend": "redis", "queued_count": 0, "dead_letter_count": 0}
            ),
            env={
                "CHATGPT2API_AUTH_KEY": "prod-long-random-key",
                "OBJECT_STORAGE_BACKEND": "r2",
                "OBJECT_STORAGE_PUBLIC_BASE_URL": "https://cdn.example.com",
                "BACKUP_OUTPUT_DIR": "/app/data/backups",
                "BACKUP_RETENTION_DAYS": "30",
                "ALERT_JOB_QUEUE_BACKLOG_THRESHOLD": "100",
                "ALERT_DISK_FREE_MB": "512",
                "ALERT_BACKUP_MAX_AGE_HOURS": "24",
                "PAYMENT_WEBHOOK_SECRET": "prod-payment-webhook-secret",
                "PAYMENT_CHECKOUT_PROVIDER": "redirect",
                "PAYMENT_CHECKOUT_URL_TEMPLATE": "https://pay.example.com/checkout?order={order_id}&sig={signature}",
                "PAYMENT_CHECKOUT_SIGNING_SECRET": "prod-checkout-signing-secret",
                "AUTH_SESSION_COOKIE_ENABLED": "true",
                "AUTH_RESPONSE_INCLUDE_TOKEN": "false",
                "EMAIL_VERIFICATION_REQUIRED": "true",
                "EMAIL_PROVIDER": "smtp",
                "SMTP_HOST": "smtp.example.com",
                "EMAIL_FROM": "ChatGPT2API <no-reply@example.com>",
                "APP_PUBLIC_URL": "https://img.example.com",
                "RATE_LIMIT_BACKEND": "redis",
                "REDIS_URL": "redis://redis:6379/0",
                "BUSINESS_LEGAL_NAME": "Example Image Inc.",
                "BUSINESS_SUPPORT_EMAIL": "support@example.com",
            },
        )

        result = service.check()

        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["ready"])
        self.assertEqual(result["summary"]["failed"], 0)

    def test_fails_for_local_development_infrastructure(self):
        service = self.create_service(
            config=FakeConfig(
                is_production=False,
                base_url="http://localhost:8000",
                origins=["http://localhost:3000"],
                hsts_enabled=False,
                auth_key="your_secret_key_here",
            ),
            storage=FakeStorage(
                info={"type": "json"},
                health={"status": "healthy"},
            ),
            object_storage=FakeObjectStorage({"backend": "local", "root_dir": "/tmp/assets"}),
            job_service=FakeJobService({"backend": "storage-polling"}),
            env={
                "OBJECT_STORAGE_BACKEND": "local",
                "BACKUP_RETENTION_DAYS": "0",
                "ALERT_JOB_QUEUE_BACKLOG_THRESHOLD": "0",
            },
        )

        result = service.check()
        failed_ids = {item["id"] for item in result["items"] if item["status"] == "failed"}

        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["ready"])
        self.assertIn("app.env.production", failed_ids)
        self.assertIn("storage.postgres", failed_ids)
        self.assertIn("queue.redis", failed_ids)
        self.assertIn("object_storage.remote", failed_ids)

    def test_system_endpoint_returns_readiness_to_admins(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        base_dir = Path(tmp_dir.name)
        storage = JSONStorageBackend(base_dir / "accounts.json", base_dir / "auth_keys.json")
        auth = AuthService(storage)
        admin_key = auth.create_key(role="admin", name="Admin")[1]

        class FakeReadiness:
            def check(self, *, strict: bool = True):
                return {"status": "passed", "ready": True, "strict": strict, "items": []}

        app = FastAPI()
        app.include_router(system.create_router("test"))
        client = TestClient(app)

        def fake_require_admin(authorization: str | None):
            if authorization == f"Bearer {admin_key}":
                return {"id": "admin", "role": "admin", "permissions": ["*"]}
            raise HTTPException(status_code=401, detail={"error": "authorization is invalid"})

        with patch.object(system, "production_readiness_service", FakeReadiness()), patch.object(system, "require_admin", fake_require_admin), patch.object(support, "auth_service", auth):
            response = client.get("/api/admin/production-readiness?strict=false", headers={"Authorization": f"Bearer {admin_key}"})
            unauthorized = client.get("/api/admin/production-readiness")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ready"])
        self.assertFalse(response.json()["strict"])
        self.assertEqual(unauthorized.status_code, 401)


if __name__ == "__main__":
    unittest.main()
