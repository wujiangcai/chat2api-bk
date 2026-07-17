from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.payment_webhook_sandbox import build_payload, canonical_body, sign_headers
from services.auth_service import AuthService
from services.billing_service import BillingService
from services.payment_webhook_service import PaymentWebhookService
from services.redemption_service import RedemptionService
from services.storage.json_storage import JSONStorageBackend


class PaymentWebhookSandboxTests(unittest.TestCase):
    def create_context(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        base_dir = Path(tmp_dir.name)
        storage = JSONStorageBackend(base_dir / "accounts.json", base_dir / "auth_keys.json")
        auth = AuthService(storage)
        redemption = RedemptionService(storage)
        billing = BillingService(storage, auth, redemption)
        user, _, _ = auth.register_user("buyer@example.com", "StrongPass123")
        package = redemption.create_package(name="Pro", quota=50, price_cents=1990)
        order = billing.create_order(user_id=str(user["id"]), email=str(user["email"]), package_id=str(package["id"]))
        return auth, billing, PaymentWebhookService(billing), user, order

    def test_stripe_sandbox_paid_payload_can_be_handled(self):
        auth, _, service, user, order = self.create_context()
        secret = "sandbox_secret"
        payload = build_payload(
            provider="stripe",
            action="paid",
            order_id=str(order["id"]),
            amount_cents=1990,
            currency="CNY",
            provider_payment_id="pi_sandbox",
            event_id="evt_sandbox_paid",
        )
        body = canonical_body(payload)
        headers = sign_headers(provider="stripe", secret=secret, body=body)

        with patch.dict("os.environ", {"PAYMENT_WEBHOOK_SECRET_STRIPE": secret}):
            result = service.handle("stripe", body, headers)

        self.assertEqual(result["action"], "mark_paid")
        self.assertEqual(result["order"]["status"], "fulfilled")
        self.assertEqual(result["payment"]["provider_payment_id"], "pi_sandbox")
        self.assertEqual(auth.list_users()[0]["quota_balance"], 50)
        ledger = auth.list_quota_ledger(user_id=str(user["id"]))
        self.assertEqual(len(ledger), 1)

    def test_wechatpay_sandbox_refund_payload_can_be_handled(self):
        auth, billing, service, user, order = self.create_context()
        billing.mark_paid(
            str(order["id"]),
            provider="wechatpay",
            provider_payment_id="wx_sandbox_paid",
            amount_cents=1990,
            actor={"role": "admin", "id": "admin"},
        )
        secret = "sandbox_secret"
        payload = build_payload(
            provider="wechatpay",
            action="refund",
            order_id=str(order["id"]),
            amount_cents=1990,
            currency="CNY",
            provider_payment_id="wx_sandbox_paid",
            event_id="wx_evt_refund",
        )
        body = canonical_body(payload)
        headers = sign_headers(provider="wechatpay", secret=secret, body=body)

        with patch.dict("os.environ", {"PAYMENT_WEBHOOK_SECRET_WECHATPAY": secret}):
            result = service.handle("wechat", body, headers)

        self.assertEqual(result["action"], "refund")
        self.assertEqual(result["order"]["status"], "refunded")
        self.assertEqual(result["payment"]["status"], "refunded")
        self.assertEqual(auth.list_users()[0]["quota_balance"], 0)
        ledger = auth.list_quota_ledger(user_id=str(user["id"]))
        self.assertEqual(len(ledger), 2)
        self.assertEqual(ledger[0]["ref_type"], "order_refund")


if __name__ == "__main__":
    unittest.main()
