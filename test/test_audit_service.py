from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import support, system
from services.audit_service import AuditService, sanitize_audit_value
from services.auth_service import AuthService
from services.storage.database_storage import AuditLogModel, DatabaseStorageBackend
from services.storage.json_storage import JSONStorageBackend


class AuditServiceTests(unittest.TestCase):
    def create_json_audit(self) -> tuple[tempfile.TemporaryDirectory[str], AuditService]:
        tmp_dir = tempfile.TemporaryDirectory()
        base_dir = Path(tmp_dir.name)
        storage = JSONStorageBackend(base_dir / "accounts.json", base_dir / "auth_keys.json")
        return tmp_dir, AuditService(storage)

    def test_record_redacts_sensitive_detail_and_supports_filters(self):
        tmp_dir, audit = self.create_json_audit()
        self.addCleanup(tmp_dir.cleanup)

        item = audit.record(
            "user.password_reset",
            actor={"role": "admin", "id": "adm_1", "email": "admin@example.com", "key_id": "key_1"},
            target_type="user",
            target_id="usr_1",
            detail={
                "password": "StrongPass123",
                "nested": {"access_token": "secret-token", "safe": "value"},
                "key_id": "key_1",
            },
        )

        self.assertEqual(item["detail"]["password"], "[REDACTED]")
        self.assertEqual(item["detail"]["nested"]["access_token"], "[REDACTED]")
        self.assertEqual(item["detail"]["nested"]["safe"], "value")
        self.assertEqual(item["detail"]["key_id"], "key_1")

        rows = audit.list_logs(action="user.password_reset", actor_id="adm_1", target_type="user", target_id="usr_1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "user.password_reset")

    def test_sanitize_truncates_large_strings(self):
        value = sanitize_audit_value({"prompt": "x" * 3000})
        self.assertTrue(str(value["prompt"]).endswith("...[TRUNCATED]"))
        self.assertLess(len(str(value["prompt"])), 2100)

    def test_database_storage_persists_audit_logs_in_dedicated_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = DatabaseStorageBackend(f"sqlite:///{(Path(tmp) / 'audit.sqlite3').as_posix()}")
            try:
                audit = AuditService(storage)
                audit.record(
                    "package.create",
                    actor={"role": "admin", "id": "adm_1"},
                    target_type="package",
                    target_id="pkg_1",
                    detail={"name": "Pro"},
                )
                rows = storage.load_collection("audit_logs")
                self.assertEqual(len(rows), 1)
                session = storage.Session()
                try:
                    self.assertEqual(session.query(AuditLogModel).count(), 1)
                    row = session.query(AuditLogModel).first()
                    self.assertEqual(row.action, "package.create")
                    self.assertEqual(row.actor_id, "adm_1")
                    self.assertEqual(row.target_type, "package")
                finally:
                    session.close()
                health = storage.health_check()
                self.assertEqual(health["dedicated_collection_counts"]["audit_logs"], 1)
            finally:
                storage.engine.dispose()

    def test_admin_audit_logs_endpoint_returns_persisted_rows(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        base_dir = Path(tmp_dir.name)
        storage = JSONStorageBackend(base_dir / "accounts.json", base_dir / "auth_keys.json")
        auth = AuthService(storage)
        audit = AuditService(storage)
        admin_key = auth.create_key(role="admin", name="Admin")[1]
        audit.record("order.fulfill", actor={"role": "admin", "id": "adm_1"}, target_type="order", target_id="ord_1")

        app = FastAPI()
        app.include_router(system.create_router("test"))
        client = TestClient(app)
        with patch.object(system, "audit_service", audit), patch.object(system, "auth_service", auth), patch.object(support, "auth_service", auth):
            response = client.get("/api/admin/audit-logs?action=order.fulfill", headers={"Authorization": f"Bearer {admin_key}"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(payload["items"][0]["target"]["id"], "ord_1")


if __name__ == "__main__":
    unittest.main()
