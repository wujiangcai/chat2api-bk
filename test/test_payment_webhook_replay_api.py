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
from services.launch_evidence_service import LaunchEvidenceService
from services.payment_webhook_service import PaymentWebhookService
from services.redemption_service import RedemptionService
from services.storage.json_storage import JSONStorageBackend


class PaymentWebhookReplayApiTests(unittest.TestCase):
    def create_context(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        base_dir = Path(tmp_dir.name)
        storage = JSONStorageBackend(base_dir / "accounts.json", base_dir / "auth_keys.json")
        auth = AuthService(storage)
        redemption = RedemptionService(storage)
        billing = BillingService(storage, auth, redemption)
        webhook_service = PaymentWebhookService(billing)
        evidence_service = LaunchEvidenceService(storage)
        app = FastAPI()
        app.include_router(system.create_router("test"))
        return {
            "client": TestClient(app),
            "auth": auth,
            "redemption": redemption,
            "billing": billing,
            "webhook_service": webhook_service,
            "evidence_service": evidence_service,
        }

    @staticmethod
    def fake_require_admin(authorization: str | None):
        if authorization == "Bearer admin-token":
            return {"id": "admin", "role": "admin", "permissions": ["*"]}
        raise HTTPException(status_code=401, detail={"error": "authorization is invalid"})

    def patch_system(self, ctx):
        return patch.multiple(
            system,
            auth_service=ctx["auth"],
            redemption_service=ctx["redemption"],
            billing_service=ctx["billing"],
            payment_webhook_service=ctx["webhook_service"],
            launch_evidence_service=ctx["evidence_service"],
            require_admin=self.fake_require_admin,
        )

    def test_admin_one_click_payment_webhook_replay_creates_order_refunds_and_archives_evidence(self):
        ctx = self.create_context()
        client: TestClient = ctx["client"]

        with self.patch_system(ctx), patch.dict("os.environ", {"PAYMENT_WEBHOOK_SECRET_STRIPE": "whsec_admin_replay"}, clear=False):
            response = client.post(
                "/api/admin/payment-webhook/replay",
                headers={"Authorization": "Bearer admin-token"},
                json={
                    "provider": "stripe",
                    "amount_cents": 1990,
                    "currency": "CNY",
                    "quota": 7,
                    "archive": True,
                    "evidence_name": "admin replay",
                },
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        report = data["report"]
        self.assertEqual(report["status"], "passed")
        self.assertTrue(report["ready"])
        self.assertTrue(report["evidence"]["payment_webhook_paid_replay"])
        self.assertTrue(report["evidence"]["payment_webhook_refund_replay"])
        self.assertEqual(data["item"]["name"], "admin replay")
        self.assertEqual(data["item"]["status"], "passed")

        order = ctx["billing"].get_order(report["order_id"])
        self.assertIsNotNone(order)
        self.assertEqual(order["status"], "refunded")
        users = ctx["auth"].list_users()
        self.assertEqual(len(users), 1)
        self.assertFalse(users[0]["enabled"])
        self.assertEqual(users[0]["quota_balance"], 0)
        packages = ctx["redemption"].list_packages()
        self.assertEqual(len(packages), 1)
        self.assertFalse(packages[0]["enabled"])
        evidence_rows = ctx["evidence_service"].list()
        self.assertEqual(len(evidence_rows), 1)
        self.assertEqual(evidence_rows[0]["source"], "admin-payment-webhook-replay")

    def test_admin_payment_webhook_replay_requires_secret(self):
        ctx = self.create_context()
        client: TestClient = ctx["client"]

        with self.patch_system(ctx), patch.dict("os.environ", {"PAYMENT_WEBHOOK_SECRET": "", "PAYMENT_WEBHOOK_SECRET_STRIPE": ""}, clear=False):
            response = client.post(
                "/api/admin/payment-webhook/replay",
                headers={"Authorization": "Bearer admin-token"},
                json={"provider": "stripe", "archive": False},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("secret", response.json()["detail"]["error"])
        self.assertEqual(ctx["billing"].list_orders({"role": "admin"}), [])

    def test_admin_checkout_webhook_replay_creates_checkout_refunds_and_archives_evidence(self):
        ctx = self.create_context()
        client: TestClient = ctx["client"]

        with self.patch_system(ctx), patch.dict("os.environ", {"PAYMENT_WEBHOOK_SECRET_STRIPE": "whsec_checkout_replay"}, clear=False):
            response = client.post(
                "/api/admin/checkout-webhook/replay",
                headers={"Authorization": "Bearer admin-token"},
                json={
                    "checkout_provider": "manual",
                    "webhook_provider": "stripe",
                    "amount_cents": 2990,
                    "currency": "CNY",
                    "quota": 9,
                    "archive": True,
                    "evidence_name": "checkout replay",
                },
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        report = data["report"]
        self.assertEqual(report["status"], "passed")
        self.assertTrue(report["ready"])
        self.assertEqual(report["source"], "admin-checkout-webhook-replay")
        self.assertEqual(report["checkout_provider"], "manual")
        self.assertEqual(report["webhook_provider"], "stripe")
        self.assertTrue(report["evidence"]["payment_checkout_order_created"])
        self.assertTrue(report["evidence"]["payment_checkout_session_created"])
        self.assertTrue(report["evidence"]["payment_checkout_webhook_replay_requested"])
        self.assertTrue(report["evidence"]["payment_checkout_paid_replay"])
        self.assertTrue(report["evidence"]["payment_checkout_refund_replay"])
        self.assertTrue(report["evidence"]["disposable_user_disabled"])
        self.assertTrue(report["evidence"]["temporary_package_disabled"])
        self.assertEqual(data["item"]["name"], "checkout replay")
        self.assertEqual(data["item"]["source"], "admin-checkout-webhook-replay")
        self.assertEqual(data["item"]["status"], "passed")

        order = ctx["billing"].get_order(report["order_id"])
        self.assertIsNotNone(order)
        self.assertEqual(order["status"], "refunded")
        checkout = order["metadata"]["checkout"]
        self.assertEqual(checkout["id"], report["checkout_id"])
        self.assertEqual(checkout["provider"], "manual")
        users = ctx["auth"].list_users()
        self.assertEqual(len(users), 1)
        self.assertFalse(users[0]["enabled"])
        self.assertEqual(users[0]["quota_balance"], 0)
        packages = ctx["redemption"].list_packages()
        self.assertEqual(len(packages), 1)
        self.assertFalse(packages[0]["enabled"])
        evidence_rows = ctx["evidence_service"].list()
        self.assertEqual(len(evidence_rows), 1)
        self.assertEqual(evidence_rows[0]["source"], "admin-checkout-webhook-replay")

    def test_admin_checkout_webhook_replay_requires_secret_before_creating_fixtures(self):
        ctx = self.create_context()
        client: TestClient = ctx["client"]

        with self.patch_system(ctx), patch.dict("os.environ", {"PAYMENT_WEBHOOK_SECRET": "", "PAYMENT_WEBHOOK_SECRET_STRIPE": ""}, clear=False):
            response = client.post(
                "/api/admin/checkout-webhook/replay",
                headers={"Authorization": "Bearer admin-token"},
                json={"checkout_provider": "manual", "webhook_provider": "stripe", "archive": False},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("secret", response.json()["detail"]["error"])
        self.assertEqual(ctx["billing"].list_orders({"role": "admin"}), [])
        self.assertEqual(ctx["auth"].list_users(), [])
        self.assertEqual(ctx["redemption"].list_packages(), [])


if __name__ == "__main__":
    unittest.main()
