from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from services.auth_service import AuthService
from services.billing_service import BillingService
from services.redemption_service import RedemptionService
from services.storage.json_storage import JSONStorageBackend


class BillingServiceTests(unittest.TestCase):
    def create_services(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        base_dir = Path(tmp_dir.name)
        storage = JSONStorageBackend(base_dir / "accounts.json", base_dir / "auth_keys.json")
        auth = AuthService(storage)
        redemption = RedemptionService(storage)
        billing = BillingService(storage, auth, redemption)
        return auth, redemption, billing

    def test_paid_order_fulfills_quota_once_by_payment_id(self):
        auth, redemption, billing = self.create_services()
        user, _, _ = auth.register_user("buyer@example.com", "StrongPass123")
        package = redemption.create_package(name="Pro", quota=25, price_cents=990, valid_days=30)
        order = billing.create_order(user_id=str(user["id"]), email=str(user["email"]), package_id=str(package["id"]))

        result = billing.mark_paid(
            str(order["id"]),
            provider="mock",
            provider_payment_id="pay-provider-1",
            amount_cents=990,
            actor={"role": "admin", "id": "admin"},
        )
        self.assertEqual(result["order"]["status"], "fulfilled")
        self.assertEqual(result["order"]["quota_granted"], 25)
        self.assertEqual(result["user"]["quota_balance"], 25)
        self.assertEqual(result["payment"]["amount_cents"], 990)

        duplicate = billing.mark_paid(
            str(order["id"]),
            provider="mock",
            provider_payment_id="pay-provider-1",
            amount_cents=990,
            actor={"role": "admin", "id": "admin"},
        )
        self.assertEqual(duplicate["order"]["status"], "fulfilled")
        self.assertTrue(duplicate["idempotent"])
        self.assertEqual(auth.list_users()[0]["quota_balance"], 25)

        ledger = auth.list_quota_ledger(user_id=str(user["id"]))
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["ref_type"], "order")
        self.assertEqual(ledger[0]["ref_id"], order["id"])
        self.assertEqual(ledger[0]["amount"], 25)

    def test_idempotency_key_conflict_cannot_pay_another_order(self):
        auth, redemption, billing = self.create_services()
        user, _, _ = auth.register_user("buyer@example.com", "StrongPass123")
        package = redemption.create_package(name="Basic", quota=10, price_cents=100)
        order1 = billing.create_order(user_id=str(user["id"]), package_id=str(package["id"]))
        order2 = billing.create_order(user_id=str(user["id"]), package_id=str(package["id"]))

        billing.mark_paid(str(order1["id"]), idempotency_key="same-key", actor={"role": "admin", "id": "admin"})
        with self.assertRaisesRegex(ValueError, "payment idempotency conflict"):
            billing.mark_paid(str(order2["id"]), idempotency_key="same-key", actor={"role": "admin", "id": "admin"})

        self.assertEqual(auth.list_users()[0]["quota_balance"], 10)

    def test_cancel_unpaid_order(self):
        auth, redemption, billing = self.create_services()
        user, _, _ = auth.register_user("buyer@example.com", "StrongPass123")
        package = redemption.create_package(name="Basic", quota=10, price_cents=100)
        order = billing.create_order(user_id=str(user["id"]), package_id=str(package["id"]))

        cancelled = billing.cancel_order(str(order["id"]), {"role": "user", "user_id": user["id"]}, reason="user")
        self.assertEqual(cancelled["status"], "cancelled")
        with self.assertRaisesRegex(ValueError, "order cannot be paid"):
            billing.mark_paid(str(order["id"]), actor={"role": "admin", "id": "admin"})

    def test_refund_fulfilled_order_deducts_quota_once(self):
        auth, redemption, billing = self.create_services()
        user, _, _ = auth.register_user("buyer@example.com", "StrongPass123")
        package = redemption.create_package(name="Pro", quota=25, price_cents=990)
        order = billing.create_order(user_id=str(user["id"]), package_id=str(package["id"]))
        paid = billing.mark_paid(str(order["id"]), provider="mock", provider_payment_id="refund-pay-1", actor={"role": "admin", "id": "admin"})
        self.assertEqual(paid["user"]["quota_balance"], 25)

        refunded = billing.refund_order(str(order["id"]), actor={"role": "admin", "id": "admin"}, reason="customer-request")
        self.assertEqual(refunded["order"]["status"], "refunded")
        self.assertEqual(refunded["payment"]["status"], "refunded")
        self.assertEqual(refunded["quota_deducted"], 25)
        self.assertEqual(refunded["user"]["quota_balance"], 0)
        self.assertFalse(refunded["idempotent"])

        duplicate = billing.refund_order(str(order["id"]), actor={"role": "admin", "id": "admin"}, reason="customer-request")
        self.assertTrue(duplicate["idempotent"])
        self.assertEqual(duplicate["user"]["quota_balance"], 0)

        ledger = auth.list_quota_ledger(user_id=str(user["id"]))
        self.assertEqual(len(ledger), 2)
        self.assertEqual(ledger[0]["ref_type"], "order_refund")
        self.assertEqual(ledger[0]["ref_id"], order["id"])
        self.assertEqual(ledger[0]["amount"], -25)
        self.assertEqual(ledger[0]["type"], "refund")

    def test_refund_requires_fulfilled_order_and_sufficient_quota(self):
        auth, redemption, billing = self.create_services()
        user, _, _ = auth.register_user("buyer@example.com", "StrongPass123")
        package = redemption.create_package(name="Basic", quota=10, price_cents=100)
        unpaid = billing.create_order(user_id=str(user["id"]), package_id=str(package["id"]))
        with self.assertRaisesRegex(ValueError, "only fulfilled orders can be refunded"):
            billing.refund_order(str(unpaid["id"]), actor={"role": "admin", "id": "admin"})

        order = billing.create_order(user_id=str(user["id"]), package_id=str(package["id"]))
        billing.mark_paid(str(order["id"]), provider="mock", provider_payment_id="refund-pay-2", actor={"role": "admin", "id": "admin"})
        self.assertTrue(auth.try_consume_user_quota(str(user["id"]), 1, reason="test-consume"))
        with self.assertRaisesRegex(ValueError, "insufficient"):
            billing.refund_order(str(order["id"]), actor={"role": "admin", "id": "admin"})
        self.assertEqual(billing.get_order(str(order["id"]))["status"], "fulfilled")
        self.assertEqual(auth.list_users()[0]["quota_balance"], 9)


if __name__ == "__main__":
    unittest.main()
