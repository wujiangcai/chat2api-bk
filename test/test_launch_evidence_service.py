from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api import support, system
from services.auth_service import AuthService
from services.launch_evidence_service import LaunchEvidenceService
from services.storage.database_storage import DatabaseStorageBackend, LaunchEvidenceModel
from services.storage.json_storage import JSONStorageBackend


class LaunchEvidenceServiceTests(unittest.TestCase):
    def create_json_context(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        base_dir = Path(tmp_dir.name)
        storage = JSONStorageBackend(base_dir / "accounts.json", base_dir / "auth_keys.json")
        return storage, LaunchEvidenceService(storage)

    def sample_report(self):
        return {
            "status": "failed",
            "ready": False,
            "base_url": "https://img.example.com",
            "generated_at": "2026-07-07T00:00:00Z",
            "summary": {"total": 3, "passed": 1, "warning": 0, "failed": 2},
            "checks": [
                {"id": "health.live", "status": "passed", "message": "ok"},
                {"id": "storage.postgresql", "status": "failed", "message": "not postgres"},
                {"id": "object_storage.remote", "status": "failed", "message": "local storage"},
            ],
            "authorization": "Bearer secret-token",
        }

    def test_create_list_get_and_delete_evidence_with_sanitized_report(self):
        _, service = self.create_json_context()

        created = service.create(
            self.sample_report(),
            actor={"role": "admin", "id": "adm_1"},
            name="staging check",
            source="remote-verifier",
        )

        self.assertEqual(created["name"], "staging check")
        self.assertEqual(created["status"], "failed")
        self.assertFalse(created["ready"])
        self.assertEqual(created["base_url"], "https://img.example.com")
        self.assertEqual(created["summary"]["failed"], 2)
        self.assertEqual(len(created["failed_checks"]), 2)
        self.assertEqual(created["report"]["authorization"], "[REDACTED]")

        rows = service.list()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("report", rows[0])

        fetched = service.get(created["id"])
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["report"]["authorization"], "[REDACTED]")

        self.assertTrue(service.delete(created["id"]))
        self.assertEqual(service.list(), [])
        self.assertFalse(service.delete(created["id"]))

    def test_database_storage_persists_launch_evidence_in_dedicated_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = DatabaseStorageBackend(f"sqlite:///{(Path(tmp) / 'launch.sqlite3').as_posix()}")
            try:
                service = LaunchEvidenceService(storage)
                created = service.create(self.sample_report(), name="prod")
                self.assertEqual(len(storage.load_collection("launch_evidence")), 1)
                session = storage.Session()
                try:
                    row = session.query(LaunchEvidenceModel).first()
                    self.assertIsNotNone(row)
                    self.assertEqual(row.id, created["id"])
                    self.assertEqual(row.status, "failed")
                    self.assertEqual(row.ready, 0)
                finally:
                    session.close()
                health = storage.health_check()
                self.assertEqual(health["dedicated_collection_counts"]["launch_evidence"], 1)
            finally:
                storage.engine.dispose()

    def test_admin_launch_evidence_endpoints(self):
        storage, evidence_service = self.create_json_context()
        auth = AuthService(storage)
        admin_key = auth.create_key(role="admin", name="Admin")[1]
        app = FastAPI()
        app.include_router(system.create_router("test"))
        client = TestClient(app)

        def fake_require_admin(authorization: str | None):
            if authorization == f"Bearer {admin_key}":
                return {"id": "admin", "role": "admin", "permissions": ["*"]}
            raise HTTPException(status_code=401, detail={"error": "authorization is invalid"})

        with patch.object(system, "launch_evidence_service", evidence_service), patch.object(system, "require_admin", fake_require_admin), patch.object(support, "auth_service", auth):
            create_response = client.post(
                "/api/admin/launch-evidence",
                headers={"Authorization": f"Bearer {admin_key}"},
                json={"name": "prod evidence", "source": "remote-verifier", "report": self.sample_report()},
            )
            list_response = client.get("/api/admin/launch-evidence", headers={"Authorization": f"Bearer {admin_key}"})
            evidence_id = create_response.json()["item"]["id"]
            get_response = client.get(f"/api/admin/launch-evidence/{evidence_id}", headers={"Authorization": f"Bearer {admin_key}"})
            delete_response = client.delete(f"/api/admin/launch-evidence/{evidence_id}", headers={"Authorization": f"Bearer {admin_key}"})
            missing_response = client.get(f"/api/admin/launch-evidence/{evidence_id}", headers={"Authorization": f"Bearer {admin_key}"})
            unauthorized = client.get("/api/admin/launch-evidence")

        self.assertEqual(create_response.status_code, 200)
        self.assertEqual(create_response.json()["item"]["report"]["authorization"], "[REDACTED]")
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(len(list_response.json()["items"]), 1)
        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(delete_response.status_code, 200)
        self.assertEqual(missing_response.status_code, 404)
        self.assertEqual(unauthorized.status_code, 401)


if __name__ == "__main__":
    unittest.main()
