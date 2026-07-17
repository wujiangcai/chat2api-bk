from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import billing as billing_api
from services.auth_service import AuthService
from services.billing_service import BillingService
from services.redemption_service import RedemptionService
from services.storage.json_storage import JSONStorageBackend


class DummyLogService:
    def add(self, *args, **kwargs):
        return None


def signed_json_headers(secret: str, body: bytes, *, timestamp: int | None = None) -> dict[str, str]:
    ts = int(timestamp or time.time())
    signature = hmac.new(secret.encode("utf-8"), f"{ts}.".encode("utf-8") + body, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Payment-Timestamp": str(ts),
        "X-Payment-Signature": f"t={ts},v1={signature}",
    }


def signed_provider_headers(
    secret: str,
    body: bytes,
    *,
    signature_header: str,
    timestamp_header: str | None = None,
    timestamp: int | None = None,
) -> dict[str, str]:
    ts = int(timestamp or time.time())
    signature = hmac.new(secret.encode("utf-8"), f"{ts}.".encode("utf-8") + body, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        signature_header: f"t={ts},v1={signature}",
    }
    if timestamp_header:
        headers[timestamp_header] = str(ts)
    return headers


class PaymentWebhookServiceTests(unittest.TestCase):
    def create_context(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        base_dir = Path(tmp_dir.name)
        storage = JSONStorageBackend(base_dir / "accounts.json", base_dir / "auth_keys.json")
        auth = AuthService(storage)
        redemption = RedemptionService(storage)
        billing = BillingService(storage, auth, redemption)
        user, _, _ = auth.register_user("buyer@example.com", "StrongPass123")
        package = redemption.create_package(name="Pro", quota=50, price_cents=1990, valid_days=30)
        order = billing.create_order(user_id=str(user["id"]), email=str(user["email"]), package_id=str(package["id"]))
        app = FastAPI()
        app.include_router(billing_api.create_router(billing))
        return {
            "client": TestClient(app),
            "auth": auth,
            "billing": billing,
            "user": user,
            "package": package,
            "order": order,
        }

    @staticmethod
    def body(payload: dict[str, object]) -> bytes:
        return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")

    def test_signed_webhook_marks_order_paid_and_fulfills_once(self):
        ctx = self.create_context()
        secret = "whsec_test_secret"
        payload = {
            "id": "evt_1",
            "type": "payment.succeeded",
            "data": {
                "object": {
                    "id": "pay_1",
                    "order_id": ctx["order"]["id"],
                    "amount_cents": 1990,
                    "currency": "CNY",
                    "metadata": {"channel": "stripe"},
                },
            },
        }
        body = self.body(payload)
        headers = signed_json_headers(secret, body)

        with patch.dict("os.environ", {"PAYMENT_WEBHOOK_SECRET": secret}), patch.object(billing_api, "log_service", DummyLogService()):
            response = ctx["client"].post("/api/payments/webhook/stripe", content=body, headers=headers)
            duplicate = ctx["client"].post("/api/payments/webhook/stripe", content=body, headers=headers)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertFalse(data["ignored"])
        self.assertEqual(data["order"]["status"], "fulfilled")
        self.assertEqual(data["order"]["provider"], "stripe")
        self.assertEqual(data["payment"]["provider_payment_id"], "pay_1")
        self.assertEqual(data["payment"]["amount_cents"], 1990)
        self.assertEqual(data["user"]["quota_balance"], 50)
        self.assertEqual(duplicate.status_code, 200)
        self.assertTrue(duplicate.json()["idempotent"])
        self.assertEqual(ctx["auth"].list_users()[0]["quota_balance"], 50)
        ledger = ctx["auth"].list_quota_ledger(user_id=ctx["user"]["id"])
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["ref_id"], ctx["order"]["id"])

    def test_stripe_signature_header_and_checkout_session_fields_are_supported(self):
        ctx = self.create_context()
        secret = "whsec_stripe_secret"
        payload = {
            "id": "evt_checkout",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_test_1",
                    "client_reference_id": ctx["order"]["id"],
                    "payment_intent": "pi_1",
                    "amount_total": 1990,
                    "currency": "cny",
                    "metadata": {"campaign": "launch"},
                },
            },
        }
        body = self.body(payload)
        headers = signed_provider_headers(secret, body, signature_header="Stripe-Signature")

        with patch.dict("os.environ", {"PAYMENT_WEBHOOK_SECRET_STRIPE": secret}), patch.object(billing_api, "log_service", DummyLogService()):
            response = ctx["client"].post("/api/payments/webhook/stripe", content=body, headers=headers)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["action"], "mark_paid")
        self.assertEqual(data["order"]["status"], "fulfilled")
        self.assertEqual(data["payment"]["provider_payment_id"], "pi_1")
        self.assertEqual(data["payment"]["currency"], "CNY")
        self.assertEqual(ctx["auth"].list_users()[0]["quota_balance"], 50)

    def test_alipay_trade_success_fields_are_supported(self):
        ctx = self.create_context()
        secret = "alipay_hmac_secret"
        payload = {
            "notify_id": "alipay_notify_1",
            "notify_type": "trade_success",
            "out_trade_no": ctx["order"]["id"],
            "trade_no": "2026070700001",
            "trade_status": "TRADE_SUCCESS",
            "total_amount": "19.90",
            "currency": "CNY",
        }
        body = self.body(payload)
        headers = signed_provider_headers(
            secret,
            body,
            signature_header="Alipay-Signature",
            timestamp_header="Alipay-Timestamp",
        )

        with patch.dict("os.environ", {"PAYMENT_WEBHOOK_SECRET_ALIPAY": secret}), patch.object(billing_api, "log_service", DummyLogService()):
            response = ctx["client"].post("/api/payments/webhook/alipay", content=body, headers=headers)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["action"], "mark_paid")
        self.assertEqual(data["order"]["status"], "fulfilled")
        self.assertEqual(data["payment"]["provider"], "alipay")
        self.assertEqual(data["payment"]["provider_payment_id"], "2026070700001")
        self.assertEqual(data["payment"]["amount_cents"], 1990)

    def test_wechatpay_decrypted_transaction_resource_fields_are_supported(self):
        ctx = self.create_context()
        secret = "wechatpay_hmac_secret"
        payload = {
            "id": "wechat_evt_1",
            "event_type": "TRANSACTION.SUCCESS",
            "resource": {
                "out_trade_no": ctx["order"]["id"],
                "transaction_id": "4200000000000001",
                "trade_state": "SUCCESS",
                "amount": {"total": 1990, "currency": "CNY"},
                "payer": {"openid": "openid-1"},
            },
        }
        body = self.body(payload)
        headers = signed_provider_headers(
            secret,
            body,
            signature_header="Wechatpay-Signature",
            timestamp_header="Wechatpay-Timestamp",
        )

        with patch.dict("os.environ", {"PAYMENT_WEBHOOK_SECRET_WECHATPAY": secret}), patch.object(billing_api, "log_service", DummyLogService()):
            response = ctx["client"].post("/api/payments/webhook/wechat", content=body, headers=headers)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["action"], "mark_paid")
        self.assertEqual(data["order"]["provider"], "wechatpay")
        self.assertEqual(data["payment"]["provider_payment_id"], "4200000000000001")
        self.assertEqual(data["payment"]["amount_cents"], 1990)

    def test_signed_refund_webhook_refunds_fulfilled_order_once(self):
        ctx = self.create_context()
        secret = "whsec_test_secret"
        ctx["billing"].mark_paid(
            str(ctx["order"]["id"]),
            provider="stripe",
            provider_payment_id="pay_for_refund",
            amount_cents=1990,
            actor={"role": "admin", "id": "admin"},
        )
        self.assertEqual(ctx["auth"].list_users()[0]["quota_balance"], 50)

        payload = {
            "id": "evt_refund_1",
            "type": "refund.succeeded",
            "data": {
                "object": {
                    "id": "re_1",
                    "order_id": ctx["order"]["id"],
                    "amount_cents": 1990,
                    "currency": "CNY",
                    "metadata": {"reason": "customer-request"},
                },
            },
        }
        body = self.body(payload)
        headers = signed_json_headers(secret, body)

        with patch.dict("os.environ", {"PAYMENT_WEBHOOK_SECRET": secret}), patch.object(billing_api, "log_service", DummyLogService()):
            response = ctx["client"].post("/api/payments/webhook/stripe", content=body, headers=headers)
            duplicate = ctx["client"].post("/api/payments/webhook/stripe", content=body, headers=headers)

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["action"], "refund")
        self.assertFalse(data["ignored"])
        self.assertEqual(data["order"]["status"], "refunded")
        self.assertEqual(data["payment"]["status"], "refunded")
        self.assertEqual(data["quota_deducted"], 50)
        self.assertEqual(data["user"]["quota_balance"], 0)
        self.assertEqual(duplicate.status_code, 200)
        self.assertTrue(duplicate.json()["idempotent"])
        self.assertEqual(ctx["auth"].list_users()[0]["quota_balance"], 0)
        ledger = ctx["auth"].list_quota_ledger(user_id=ctx["user"]["id"])
        self.assertEqual(len(ledger), 2)
        self.assertEqual(ledger[0]["ref_type"], "order_refund")
        self.assertEqual(ledger[0]["amount"], -50)

    def test_webhook_rejects_bad_signature_without_fulfillment(self):
        ctx = self.create_context()
        secret = "whsec_test_secret"
        payload = {
            "id": "evt_bad",
            "type": "payment.succeeded",
            "data": {"object": {"id": "pay_bad", "order_id": ctx["order"]["id"], "amount_cents": 1990}},
        }
        body = self.body(payload)
        headers = signed_json_headers("wrong-secret", body)

        with patch.dict("os.environ", {"PAYMENT_WEBHOOK_SECRET": secret}), patch.object(billing_api, "log_service", DummyLogService()):
            response = ctx["client"].post("/api/payments/webhook/mockpay", content=body, headers=headers)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(ctx["billing"].get_order(str(ctx["order"]["id"]))["status"], "pending_payment")
        self.assertEqual(ctx["auth"].list_users()[0]["quota_balance"], 0)
        self.assertEqual(ctx["auth"].list_quota_ledger(user_id=ctx["user"]["id"]), [])

    def test_non_success_webhook_is_verified_but_ignored(self):
        ctx = self.create_context()
        secret = "whsec_test_secret"
        payload = {
            "id": "evt_pending",
            "type": "payment.processing",
            "data": {"object": {"id": "pay_pending", "order_id": ctx["order"]["id"], "status": "pending"}},
        }
        body = self.body(payload)
        headers = signed_json_headers(secret, body)

        with patch.dict("os.environ", {"PAYMENT_WEBHOOK_SECRET": secret}), patch.object(billing_api, "log_service", DummyLogService()):
            response = ctx["client"].post("/api/payments/webhook/mockpay", content=body, headers=headers)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ignored"])
        self.assertEqual(ctx["billing"].get_order(str(ctx["order"]["id"]))["status"], "pending_payment")

    def test_pending_refund_webhook_is_ignored_until_successful(self):
        ctx = self.create_context()
        secret = "whsec_test_secret"
        ctx["billing"].mark_paid(
            str(ctx["order"]["id"]),
            provider="stripe",
            provider_payment_id="pay_pending_refund",
            amount_cents=1990,
            actor={"role": "admin", "id": "admin"},
        )
        payload = {
            "id": "evt_refund_pending",
            "type": "refund.created",
            "data": {
                "object": {
                    "id": "re_pending",
                    "order_id": ctx["order"]["id"],
                    "status": "pending",
                    "amount_cents": 1990,
                },
            },
        }
        body = self.body(payload)
        headers = signed_json_headers(secret, body)

        with patch.dict("os.environ", {"PAYMENT_WEBHOOK_SECRET": secret}), patch.object(billing_api, "log_service", DummyLogService()):
            response = ctx["client"].post("/api/payments/webhook/stripe", content=body, headers=headers)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ignored"])
        self.assertEqual(response.json()["action"], "ignore")
        self.assertEqual(ctx["billing"].get_order(str(ctx["order"]["id"]))["status"], "fulfilled")
        self.assertEqual(ctx["auth"].list_users()[0]["quota_balance"], 50)


if __name__ == "__main__":
    unittest.main()
