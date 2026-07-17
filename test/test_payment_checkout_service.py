from __future__ import annotations

import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.auth_service import AuthService
from services.billing_service import BillingService
from services.payment_checkout_service import PaymentCheckoutError, PaymentCheckoutService
from services.redemption_service import RedemptionService
from services.storage.json_storage import JSONStorageBackend


class FakeConfig:
    base_url = "https://img.example.com"
    web_allowed_origins = ["https://img.example.com"]


class FakeStripeResponse:
    def __init__(self, payload: dict[str, object]):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class PaymentCheckoutServiceTests(unittest.TestCase):
    def create_services(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        base_dir = Path(tmp_dir.name)
        storage = JSONStorageBackend(base_dir / "accounts.json", base_dir / "auth_keys.json")
        auth = AuthService(storage)
        redemption = RedemptionService(storage)
        billing = BillingService(storage, auth, redemption)
        user, _, _ = auth.register_user("buyer@example.com", "StrongPass123")
        other_user, _, _ = auth.register_user("other@example.com", "StrongPass123")
        package = redemption.create_package(name="Pro Pack", quota=25, price_cents=990, valid_days=30)
        order = billing.create_order(user_id=str(user["id"]), email=str(user["email"]), package_id=str(package["id"]))
        return auth, billing, user, other_user, package, order

    def test_manual_checkout_attaches_instructions_to_unpaid_order(self):
        _, billing, user, _, _, order = self.create_services()
        service = PaymentCheckoutService(
            billing,
            env={
                "PAYMENT_CHECKOUT_PROVIDER": "manual",
                "PAYMENT_CHECKOUT_MANUAL_INSTRUCTIONS": "Transfer and write order {order_id_raw}",
                "PAYMENT_CHECKOUT_MANUAL_URL": "https://pay.example.com/manual?order={order_id}",
            },
            config_obj=FakeConfig(),
        )

        result = service.create_checkout(str(order["id"]), {"role": "user", "user_id": user["id"]})

        self.assertEqual(result["checkout"]["provider"], "manual")
        self.assertEqual(result["checkout"]["mode"], "manual")
        self.assertIn(str(order["id"]), result["checkout"]["instructions"])
        self.assertIn(str(order["id"]), result["checkout"]["payment_url"])
        self.assertEqual(result["order"]["metadata"]["checkout"]["id"], result["checkout"]["id"])
        self.assertEqual(result["order"]["provider"], "manual")

    def test_redirect_checkout_uses_signed_template_and_rejects_other_user(self):
        _, billing, user, other_user, _, order = self.create_services()
        service = PaymentCheckoutService(
            billing,
            env={
                "PAYMENT_CHECKOUT_PROVIDER": "redirect",
                "PAYMENT_CHECKOUT_URL_TEMPLATE": "https://pay.example.com/checkout?order={order_id}&amount={amount_cents}&sig={signature}&success={success_url}",
                "PAYMENT_CHECKOUT_SIGNING_SECRET": "checkout-secret",
                "APP_PUBLIC_URL": "https://img.example.com",
                "WEB_ALLOWED_ORIGINS": "https://img.example.com",
            },
            config_obj=FakeConfig(),
        )

        with self.assertRaisesRegex(PaymentCheckoutError, "order not found"):
            service.create_checkout(str(order["id"]), {"role": "user", "user_id": other_user["id"]})

        result = service.create_checkout(
            str(order["id"]),
            {"role": "user", "user_id": user["id"]},
            success_url="https://img.example.com/redeem?ok=1",
            cancel_url="https://evil.example.com/cancel",
        )

        self.assertEqual(result["checkout"]["provider"], "redirect")
        self.assertIn("https://pay.example.com/checkout", result["checkout"]["payment_url"])
        self.assertIn("sig=", result["checkout"]["payment_url"])
        self.assertIn("success=https%3A%2F%2Fimg.example.com%2Fredeem%3Fok%3D1", result["checkout"]["payment_url"])
        self.assertEqual(result["checkout"]["cancel_url"], "https://img.example.com/redeem?checkout=cancel&order_id=" + str(order["id"]))

    def test_stripe_checkout_creates_hosted_session_with_order_metadata(self):
        _, billing, user, _, _, order = self.create_services()
        service = PaymentCheckoutService(
            billing,
            env={
                "PAYMENT_CHECKOUT_PROVIDER": "stripe",
                "STRIPE_SECRET_KEY": "sk_test_123",
                "APP_PUBLIC_URL": "https://img.example.com",
                "WEB_ALLOWED_ORIGINS": "https://img.example.com",
            },
            config_obj=FakeConfig(),
        )
        captured = {}

        def fake_urlopen(request, timeout=0):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeStripeResponse({
                "id": "cs_test_123",
                "url": "https://checkout.stripe.com/c/pay/cs_test_123",
                "payment_status": "unpaid",
                "livemode": False,
                "expires_at": 1780000000,
            })

        with patch("services.payment_checkout_service.urllib.request.urlopen", fake_urlopen):
            result = service.create_checkout(str(order["id"]), {"role": "user", "user_id": user["id"]})

        request = captured["request"]
        body = request.data.decode("utf-8")
        self.assertEqual(result["checkout"]["provider"], "stripe")
        self.assertEqual(result["checkout"]["provider_session_id"], "cs_test_123")
        self.assertEqual(result["checkout"]["payment_url"], "https://checkout.stripe.com/c/pay/cs_test_123")
        self.assertIn(f"client_reference_id={order['id']}", body)
        self.assertIn(f"metadata%5Border_id%5D={order['id']}", body)
        expected_auth = "Basic " + base64.b64encode(b"sk_test_123:").decode("ascii")
        self.assertEqual(request.headers["Authorization"], expected_auth)
        self.assertEqual(request.headers["Idempotency-key"], f"checkout:{order['id']}")

    def test_checkout_rejects_paid_or_cancelled_orders(self):
        _, billing, user, _, _, order = self.create_services()
        billing.mark_paid(str(order["id"]), provider="mock", provider_payment_id="pay-1", actor={"role": "admin", "id": "admin"})
        service = PaymentCheckoutService(billing, env={"PAYMENT_CHECKOUT_PROVIDER": "manual"}, config_obj=FakeConfig())

        with self.assertRaisesRegex(PaymentCheckoutError, "only unpaid orders"):
            service.create_checkout(str(order["id"]), {"role": "user", "user_id": user["id"]})


if __name__ == "__main__":
    unittest.main()
