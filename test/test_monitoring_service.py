from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api import support, system
from services.auth_service import AuthService
from services.monitoring_service import MonitoringService
from services.storage.json_storage import JSONStorageBackend


class FakeObjectStorage:
    def info(self):
        return {"backend": "local", "root_dir": "/tmp/assets"}


class FakeJobService:
    def queue_info(self):
        return {"backend": "storage-polling"}


class MonitoringServiceTests(unittest.TestCase):
    def create_storage(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        base_dir = Path(tmp_dir.name)
        storage = JSONStorageBackend(base_dir / "accounts.json", base_dir / "auth_keys.json")
        return base_dir, storage

    def test_collect_metrics_and_dead_letter_alerts(self):
        _, storage = self.create_storage()
        storage.save_collection("users", [{"id": "usr_1", "email": "u@example.com", "password_hash": "x", "enabled": True}])
        storage.save_collection("orders", [{"id": "ord_1", "user_id": "usr_1", "package_id": "pkg_1", "status": "pending_payment"}])
        storage.save_collection(
            "image_jobs",
            [
                {"id": "job_1", "status": "queued", "request": {"prompt": "a"}},
                {"id": "job_2", "status": "failed", "dead_lettered_at": "2026-07-07T00:00:00+00:00", "request": {"prompt": "b"}},
            ],
        )
        service = MonitoringService(
            storage_factory=lambda: storage,
            object_storage_factory=lambda: FakeObjectStorage(),
            image_job_service_factory=lambda: FakeJobService(),
            data_dir=Path(tempfile.gettempdir()),
        )

        snapshot = service.collect()

        self.assertEqual(snapshot["metrics"]["users_total"], 1)
        self.assertEqual(snapshot["metrics"]["orders_status_pending_payment_total"], 1)
        self.assertEqual(snapshot["metrics"]["image_jobs_status_queued_total"], 1)
        self.assertEqual(snapshot["metrics"]["image_jobs_dead_letter_total"], 1)
        self.assertTrue(any(alert["code"] == "image_job_dead_letter" for alert in snapshot["alerts"]))
        prometheus = service.prometheus_text(snapshot)
        self.assertIn("chatgpt2api_image_jobs_dead_letter_total 1", prometheus)

    def test_support_ticket_sla_overdue_alerts(self):
        _, storage = self.create_storage()
        old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        due = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        storage.save_collection(
            "support_tickets",
            [
                {
                    "id": "tic_overdue",
                    "user_id": "usr_1",
                    "email": "u@example.com",
                    "subject": "Help",
                    "status": "open",
                    "priority": "high",
                    "created_at": old,
                    "updated_at": old,
                    "first_response_due_at": due,
                    "resolution_due_at": due,
                    "messages": [
                        {
                            "id": "msg_1",
                            "body": "help",
                            "created_at": old,
                            "attachments": [{"id": "att_1", "filename": "screenshot.png"}],
                        }
                    ],
                }
            ],
        )
        service = MonitoringService(storage_factory=lambda: storage, data_dir=Path(tempfile.gettempdir()))

        snapshot = service.collect()

        self.assertEqual(snapshot["metrics"]["support_tickets_total"], 1)
        self.assertEqual(snapshot["metrics"]["support_tickets_attachments_total"], 1)
        self.assertEqual(snapshot["metrics"]["support_tickets_response_overdue_total"], 1)
        self.assertEqual(snapshot["metrics"]["support_tickets_resolution_overdue_total"], 1)
        alert_codes = {item["code"] for item in snapshot["alerts"]}
        self.assertIn("support_ticket_response_overdue", alert_codes)
        self.assertIn("support_ticket_resolution_overdue", alert_codes)

    def test_readiness_is_unhealthy_when_storage_check_fails(self):
        service = MonitoringService(storage_factory=lambda: (_ for _ in ()).throw(RuntimeError("db down")))

        readiness = service.readiness()

        self.assertEqual(readiness["status"], "unhealthy")
        self.assertTrue(any(alert["code"] == "storage_unhealthy" for alert in readiness["alerts"]))

    def test_system_monitoring_endpoints(self):
        _, storage = self.create_storage()
        auth = AuthService(storage)
        admin_key = auth.create_key(role="admin", name="Admin")[1]
        monitoring = MonitoringService(
            storage_factory=lambda: storage,
            object_storage_factory=lambda: FakeObjectStorage(),
            image_job_service_factory=lambda: FakeJobService(),
            data_dir=Path(tempfile.gettempdir()),
        )
        app = FastAPI()
        app.include_router(system.create_router("test"))
        client = TestClient(app)

        def fake_require_admin(authorization: str | None):
            if authorization == f"Bearer {admin_key}":
                return {"id": "admin", "role": "admin", "permissions": ["*"]}
            raise HTTPException(status_code=401, detail={"error": "authorization is invalid"})

        with patch.object(system, "monitoring_service", monitoring), patch.object(system, "require_admin", fake_require_admin), patch.object(system, "auth_service", auth), patch.object(support, "auth_service", auth):
            live = client.get("/health/live")
            ready = client.get("/health/ready")
            metrics = client.get("/api/admin/metrics", headers={"Authorization": f"Bearer {admin_key}"})
            prom = client.get("/api/admin/metrics?format=prometheus", headers={"Authorization": f"Bearer {admin_key}"})
            alerts = client.get("/api/admin/alerts", headers={"Authorization": f"Bearer {admin_key}"})
            unauthorized = client.get("/api/admin/metrics")

        self.assertEqual(live.status_code, 200)
        self.assertEqual(live.json()["version"], "test")
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(metrics.status_code, 200)
        self.assertIn("metrics", metrics.json())
        self.assertEqual(prom.status_code, 200)
        self.assertIn("chatgpt2api_up", prom.text)
        self.assertEqual(alerts.status_code, 200)
        self.assertEqual(unauthorized.status_code, 401)


if __name__ == "__main__":
    unittest.main()
