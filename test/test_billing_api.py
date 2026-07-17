from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import billing as billing_api
from api import support
from services.auth_service import AuthService
from services.billing_service import BillingService
from services.payment_checkout_service import PaymentCheckoutService
from services.redemption_service import RedemptionService
from services.storage.json_storage import JSONStorageBackend


class DummyLogService:
    def add(self, *args, **kwargs):
        return None


class BillingApiTests(unittest.TestCase):
    def create_client(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        base_dir = Path(tmp_dir.name)
        storage = JSONStorageBackend(base_dir / "accounts.json", base_dir / "auth_keys.json")
        auth = AuthService(storage)
        redemption = RedemptionService(storage)
        billing = BillingService(storage, auth, redemption)
        admin_key = auth.create_key(role="admin", name="Admin")[1]
        user, user_token, _ = auth.register_user("buyer@example.com", "StrongPass123")
        other_user, other_token, _ = auth.register_user("other@example.com", "StrongPass123")
        package = redemption.create_package(name="Pro", quota=50, price_cents=1990, valid_days=30)
        checkout = PaymentCheckoutService(
            billing,
            env={
                "PAYMENT_CHECKOUT_PROVIDER": "redirect",
                "PAYMENT_CHECKOUT_URL_TEMPLATE": "https://pay.example.com/checkout?order={order_id}&amount={amount_cents}&sig={signature}",
                "PAYMENT_CHECKOUT_SIGNING_SECRET": "checkout-secret",
                "APP_PUBLIC_URL": "https://img.example.com",
                "WEB_ALLOWED_ORIGINS": "https://img.example.com",
            },
        )
        app = FastAPI()
        app.include_router(billing_api.create_router(billing, checkout_service=checkout))
        return {
            "client": TestClient(app),
            "auth": auth,
            "billing": billing,
            "admin_key": admin_key,
            "user": user,
            "user_token": user_token,
            "other_user": other_user,
            "other_token": other_token,
            "package": package,
        }

    def test_user_order_admin_mark_paid_and_idempotent_notify(self):
        ctx = self.create_client()
        client: TestClient = ctx["client"]
        admin_headers = {"Authorization": f"Bearer {ctx['admin_key']}"}
        user_headers = {"Authorization": f"Bearer {ctx['user_token']}"}
        other_headers = {"Authorization": f"Bearer {ctx['other_token']}"}

        with patch.multiple(support, auth_service=ctx["auth"]), patch.object(billing_api, "log_service", DummyLogService()):
            packages_response = client.get("/api/packages", headers=user_headers)
            self.assertEqual(packages_response.status_code, 200)
            self.assertEqual(packages_response.json()["items"][0]["id"], ctx["package"]["id"])

            create_response = client.post(
                "/api/orders",
                headers=user_headers,
                json={"package_id": ctx["package"]["id"]},
            )
            self.assertEqual(create_response.status_code, 200)
            order = create_response.json()["order"]
            self.assertEqual(order["status"], "pending_payment")
            self.assertEqual(order["quota_total"], 50)
            self.assertEqual(order["amount_cents"], 1990)

            checkout_response = client.post(
                f"/api/orders/{order['id']}/checkout",
                headers=user_headers,
                json={"provider": "redirect"},
            )
            self.assertEqual(checkout_response.status_code, 200)
            checkout_data = checkout_response.json()
            self.assertEqual(checkout_data["checkout"]["provider"], "redirect")
            self.assertIn(order["id"], checkout_data["checkout"]["payment_url"])
            self.assertIn("sig=", checkout_data["checkout"]["payment_url"])
            self.assertEqual(checkout_data["order"]["metadata"]["checkout"]["id"], checkout_data["checkout"]["id"])

            forbidden_response = client.get(f"/api/orders/{order['id']}", headers=other_headers)
            self.assertEqual(forbidden_response.status_code, 404)

            forbidden_checkout = client.post(f"/api/orders/{order['id']}/checkout", headers=other_headers, json={})
            self.assertEqual(forbidden_checkout.status_code, 404)

            admin_orders_response = client.get("/api/admin/orders", headers=admin_headers)
            self.assertEqual(admin_orders_response.status_code, 200)
            self.assertEqual(admin_orders_response.json()["items"][0]["id"], order["id"])

            paid_response = client.post(
                f"/api/admin/orders/{order['id']}/mark-paid",
                headers=admin_headers,
                json={
                    "provider": "mock",
                    "provider_payment_id": "mock-payment-1",
                    "amount_cents": 1990,
                    "currency": "CNY",
                },
            )
            self.assertEqual(paid_response.status_code, 200)
            paid_data = paid_response.json()
            self.assertEqual(paid_data["order"]["status"], "fulfilled")
            self.assertEqual(paid_data["order"]["quota_granted"], 50)
            self.assertEqual(paid_data["user"]["quota_balance"], 50)

            duplicate_response = client.post(
                "/api/payments/mock/notify",
                headers=admin_headers,
                json={
                    "order_id": order["id"],
                    "provider": "mock",
                    "provider_payment_id": "mock-payment-1",
                    "amount_cents": 1990,
                },
            )
            self.assertEqual(duplicate_response.status_code, 200)
            self.assertTrue(duplicate_response.json()["idempotent"])
            self.assertEqual(ctx["auth"].list_users()[0]["quota_balance"], 50)

            ledger = ctx["auth"].list_quota_ledger(user_id=ctx["user"]["id"])
            self.assertEqual(len(ledger), 1)
            self.assertEqual(ledger[0]["ref_type"], "order")
            self.assertEqual(ledger[0]["ref_id"], order["id"])

            receipt_response = client.get(f"/api/orders/{order['id']}/receipt", headers=user_headers)
            self.assertEqual(receipt_response.status_code, 200)
            receipt = receipt_response.json()["receipt"]
            self.assertEqual(receipt["order_id"], order["id"])
            self.assertEqual(receipt["payment_id"], paid_data["payment"]["id"])
            self.assertEqual(receipt["total_cents"], 1990)
            self.assertEqual(receipt["buyer"]["email"], "buyer@example.com")
            self.assertTrue(receipt["receipt_number"].startswith("RCPT-"))

            other_receipt = client.get(f"/api/orders/{order['id']}/receipt", headers=other_headers)
            self.assertEqual(other_receipt.status_code, 404)

            admin_receipt_html = client.get(f"/api/admin/orders/{order['id']}/receipt?format=html", headers=admin_headers)
            self.assertEqual(admin_receipt_html.status_code, 200)
            self.assertIn("Receipt / 收据", admin_receipt_html.text)


            refund_response = client.post(
                f"/api/admin/orders/{order['id']}/refund",
                headers=admin_headers,
                json={"reason": "customer-request", "metadata": {"ticket_id": "T-1"}},
            )
            self.assertEqual(refund_response.status_code, 200)
            refund_data = refund_response.json()
            self.assertEqual(refund_data["order"]["status"], "refunded")
            self.assertEqual(refund_data["payment"]["status"], "refunded")
            self.assertEqual(refund_data["quota_deducted"], 50)
            self.assertEqual(refund_data["user"]["quota_balance"], 0)

            duplicate_refund = client.post(
                f"/api/admin/orders/{order['id']}/refund",
                headers=admin_headers,
                json={"reason": "customer-request"},
            )
            self.assertEqual(duplicate_refund.status_code, 200)
            self.assertTrue(duplicate_refund.json()["idempotent"])
            self.assertEqual(ctx["auth"].list_users()[0]["quota_balance"], 0)

            refund_receipt_response = client.get(f"/api/orders/{order['id']}/receipt", headers=user_headers)
            self.assertEqual(refund_receipt_response.status_code, 200)
            refund_receipt = refund_receipt_response.json()["receipt"]
            self.assertEqual(refund_receipt["receipt_type"], "refund")
            self.assertTrue(refund_receipt["receipt_number"].startswith("RFND-"))
            self.assertEqual(refund_receipt["total_cents"], -1990)
            self.assertEqual(refund_receipt["quota_deducted"], 50)

            ledger_after_refund = ctx["auth"].list_quota_ledger(user_id=ctx["user"]["id"])
            self.assertEqual(len(ledger_after_refund), 2)
            self.assertEqual(ledger_after_refund[0]["ref_type"], "order_refund")
            self.assertEqual(ledger_after_refund[0]["amount"], -50)

    def test_user_can_cancel_only_own_unpaid_order(self):
        ctx = self.create_client()
        client: TestClient = ctx["client"]
        user_headers = {"Authorization": f"Bearer {ctx['user_token']}"}
        other_headers = {"Authorization": f"Bearer {ctx['other_token']}"}

        with patch.multiple(support, auth_service=ctx["auth"]), patch.object(billing_api, "log_service", DummyLogService()):
            order = client.post(
                "/api/orders",
                headers=user_headers,
                json={"package_id": ctx["package"]["id"]},
            ).json()["order"]
            forbidden_response = client.post(f"/api/orders/{order['id']}/cancel", headers=other_headers, json={"reason": "no"})
            self.assertEqual(forbidden_response.status_code, 404)

            cancel_response = client.post(f"/api/orders/{order['id']}/cancel", headers=user_headers, json={"reason": "changed"})
            self.assertEqual(cancel_response.status_code, 200)
            self.assertEqual(cancel_response.json()["order"]["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
