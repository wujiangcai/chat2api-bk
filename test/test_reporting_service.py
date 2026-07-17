from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api import system
from services.auth_service import AuthService
from services.billing_service import BillingService
from services.redemption_service import RedemptionService
from services.reporting_service import ReportingService
from services.storage.json_storage import JSONStorageBackend


class ReportingServiceTests(unittest.TestCase):
    def create_context(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        base_dir = Path(tmp_dir.name)
        storage = JSONStorageBackend(base_dir / "accounts.json", base_dir / "auth_keys.json")
        auth = AuthService(storage)
        redemption = RedemptionService(storage)
        billing = BillingService(storage, auth, redemption)
        report = ReportingService(lambda: storage)
        return storage, auth, redemption, billing, report

    def seed_business_data(self):
        storage, auth, redemption, billing, report = self.create_context()
        user, _, _ = auth.register_user("buyer@example.com", "StrongPass123")
        disabled, _, _ = auth.register_user("disabled@example.com", "StrongPass123")
        auth.update_user(str(disabled["id"]), {"enabled": False})
        package = redemption.create_package(name="Pro", quota=25, price_cents=1000, valid_days=30)
        order = billing.create_order(user_id=str(user["id"]), email=str(user["email"]), package_id=str(package["id"]))
        billing.mark_paid(
            str(order["id"]),
            provider="mock",
            provider_payment_id="provider-pay-1",
            amount_cents=1000,
            actor={"role": "admin", "id": "admin"},
        )
        self.assertTrue(auth.try_consume_user_quota(str(user["id"]), 2, ref_type="image_job", ref_id="job_succeeded"))
        storage.save_collection(
            "image_jobs",
            [
                {
                    "id": "job_succeeded",
                    "status": "succeeded",
                    "request": {"n": 3},
                    "reserved_quota": 3,
                    "refunded_quota": 0,
                    "cost_units": 3,
                    "attempts": 1,
                    "created_at": "2026-07-01T00:00:00+00:00",
                },
                {
                    "id": "job_failed",
                    "status": "failed",
                    "reserved_quota": 1,
                    "refunded_quota": 1,
                    "cost_units": 0,
                    "attempts": 2,
                    "dead_lettered_at": "2026-07-01T00:05:00+00:00",
                    "error": {"dead_lettered": True},
                    "created_at": "2026-07-01T00:01:00+00:00",
                },
            ],
        )
        storage.save_collection(
            "image_assets",
            [
                {"id": "asset_active", "status": "active", "size_bytes": 512, "created_at": "2026-07-01T00:00:00+00:00"},
                {"id": "asset_deleted", "status": "deleted", "size_bytes": 128, "created_at": "2026-07-01T00:00:00+00:00"},
            ],
        )
        return storage, auth, redemption, billing, report

    def test_collects_business_report_with_revenue_cost_and_quality_metrics(self):
        _, _, _, _, report_service = self.seed_business_data()

        with patch.dict("os.environ", {"COST_PER_IMAGE_CENTS": "12", "COST_CURRENCY": "CNY"}):
            report = report_service.collect(days=30)

        self.assertEqual(report["window_days"], 30)
        self.assertEqual(report["summary"]["users_total"], 2)
        self.assertEqual(report["summary"]["users_enabled_total"], 1)
        self.assertEqual(report["summary"]["gross_revenue_cents_by_currency"], {"CNY": 1000})
        self.assertEqual(report["summary"]["estimated_image_cost_cents"], 36)
        self.assertEqual(report["summary"]["estimated_gross_margin_cents_by_currency"], {"CNY": 964})
        self.assertEqual(report["summary"]["quota_balance_total"], 23)
        self.assertEqual(report["all_time"]["orders"]["fulfilled_total"], 1)
        self.assertEqual(report["all_time"]["payments"]["succeeded_total"], 1)
        self.assertEqual(report["all_time"]["quota"]["granted_units"], 25)
        self.assertEqual(report["all_time"]["quota"]["consumed_units"], 2)
        self.assertEqual(report["all_time"]["image_jobs"]["success_rate"], 0.5)
        self.assertEqual(report["all_time"]["image_jobs"]["dead_letter_total"], 1)
        self.assertEqual(report["all_time"]["image_jobs"]["cost_units"], 3)
        self.assertEqual(report["all_time"]["image_assets"]["active_size_bytes"], 512)
        self.assertEqual(report["window"]["payments"]["gross_revenue_cents_by_currency"], {"CNY": 1000})

    def test_refunded_orders_report_zero_net_revenue_and_refund_quota(self):
        _, auth, redemption, billing, report_service = self.create_context()
        user, _, _ = auth.register_user("buyer@example.com", "StrongPass123")
        package = redemption.create_package(name="Basic", quota=10, price_cents=1000)
        order = billing.create_order(user_id=str(user["id"]), package_id=str(package["id"]))
        billing.mark_paid(str(order["id"]), provider="mock", provider_payment_id="report-refund-1", actor={"role": "admin", "id": "admin"})
        billing.refund_order(str(order["id"]), actor={"role": "admin", "id": "admin"}, reason="report-test")

        report = report_service.collect(days=30)

        self.assertEqual(report["all_time"]["orders"]["refunded_total"], 1)
        self.assertEqual(report["all_time"]["payments"]["refunded_total"], 1)
        self.assertEqual(report["all_time"]["payments"]["gross_revenue_cents_by_currency"], {"CNY": 1000})
        self.assertEqual(report["all_time"]["payments"]["refunded_cents_by_currency"], {"CNY": 1000})
        self.assertEqual(report["all_time"]["payments"]["net_revenue_cents_by_currency"], {"CNY": 0})
        self.assertEqual(report["all_time"]["quota"]["refunded_units"], 10)
        self.assertEqual(report["all_time"]["quota"]["net_units"], 0)

    def test_admin_business_report_endpoint_requires_admin(self):
        _, _, _, _, report_service = self.seed_business_data()
        app = FastAPI()
        app.include_router(system.create_router("test"))
        client = TestClient(app)

        def fake_require_admin(authorization: str | None):
            if authorization == "Bearer admin-token":
                return {"id": "admin", "role": "admin", "permissions": ["*"]}
            raise HTTPException(status_code=401, detail={"error": "authorization is invalid"})

        with patch.object(system, "reporting_service", report_service), patch.object(system, "require_admin", fake_require_admin), patch.dict("os.environ", {"COST_PER_IMAGE_CENTS": "12"}):
            response = client.get("/api/admin/business-report?days=7", headers={"Authorization": "Bearer admin-token"})
            unauthorized = client.get("/api/admin/business-report")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["summary"]["gross_revenue_cents_by_currency"], {"CNY": 1000})
        self.assertEqual(response.json()["window_days"], 7)
        self.assertEqual(unauthorized.status_code, 401)


if __name__ == "__main__":
    unittest.main()
