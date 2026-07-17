from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from services.auth_service import AuthService
from services.billing_service import BillingService
from services.redemption_service import RedemptionService
from services.storage.database_storage import (
    AuthActionTokenModel,
    AuthSessionModel,
    CDKModel,
    CollectionItemModel,
    DatabaseStorageBackend,
    OrderModel,
    PackageModel,
    PaymentModel,
    QuotaLedgerModel,
    SupportTicketModel,
    UserModel,
)


class DatabaseStorageCommercialTests(unittest.TestCase):
    def create_storage(self) -> DatabaseStorageBackend:
        tmp_dir = tempfile.TemporaryDirectory()
        db_path = Path(tmp_dir.name) / "commercial.sqlite3"
        storage = DatabaseStorageBackend(f"sqlite:///{db_path.as_posix()}")
        self.addCleanup(tmp_dir.cleanup)
        self.addCleanup(storage.engine.dispose)
        return storage

    def test_dedicated_order_and_payment_tables_are_used(self):
        storage = self.create_storage()
        order = {
            "id": "ord_1",
            "user_id": "usr_1",
            "email": "buyer@example.com",
            "package_id": "pkg_1",
            "status": "pending_payment",
            "amount_cents": 990,
            "currency": "CNY",
            "created_at": "2026-07-07T00:00:00+00:00",
            "updated_at": "2026-07-07T00:00:00+00:00",
        }
        payment = {
            "id": "pay_1",
            "order_id": "ord_1",
            "user_id": "usr_1",
            "email": "buyer@example.com",
            "provider": "mock",
            "provider_payment_id": "provider-pay-1",
            "idempotency_key": "idem-1",
            "amount_cents": 990,
            "currency": "CNY",
            "status": "succeeded",
            "created_at": "2026-07-07T00:01:00+00:00",
            "paid_at": "2026-07-07T00:01:00+00:00",
        }

        storage.save_collection("orders", [order])
        storage.append_collection_item("payments", payment)

        self.assertEqual(storage.load_collection("orders"), [order])
        self.assertEqual(storage.load_collection("payments"), [payment])
        session = storage.Session()
        try:
            self.assertEqual(session.query(OrderModel).count(), 1)
            self.assertEqual(session.query(PaymentModel).count(), 1)
            self.assertEqual(session.query(CollectionItemModel).filter(CollectionItemModel.collection.in_(["orders", "payments"])).count(), 0)
            self.assertEqual(session.query(OrderModel).first().status, "pending_payment")
            self.assertEqual(session.query(PaymentModel).first().provider_payment_id, "provider-pay-1")
        finally:
            session.close()

        health = storage.health_check()
        self.assertEqual(health["status"], "healthy")
        self.assertEqual(health["dedicated_collection_counts"]["orders"], 1)
        self.assertEqual(health["dedicated_collection_counts"]["payments"], 1)

    def test_legacy_generic_collection_is_migrated_to_dedicated_table(self):
        storage = self.create_storage()
        legacy_order = {
            "id": "ord_legacy",
            "user_id": "usr_legacy",
            "package_id": "pkg_legacy",
            "status": "pending_payment",
            "amount_cents": 100,
            "created_at": "2026-07-07T00:00:00+00:00",
            "updated_at": "2026-07-07T00:00:00+00:00",
        }
        session = storage.Session()
        try:
            session.add(CollectionItemModel(collection="orders", item_id="ord_legacy", data=json.dumps(legacy_order)))
            session.commit()
        finally:
            session.close()

        self.assertEqual(storage.load_collection("orders"), [legacy_order])
        session = storage.Session()
        try:
            self.assertEqual(session.query(OrderModel).count(), 1)
            self.assertEqual(session.query(OrderModel).first().id, "ord_legacy")
        finally:
            session.close()

    def test_auth_redemption_billing_use_dedicated_database_tables(self):
        storage = self.create_storage()
        auth = AuthService(storage)
        redemption = RedemptionService(storage)
        billing = BillingService(storage, auth, redemption)

        user, _, _ = auth.register_user("buyer@example.com", "StrongPass123")
        package = redemption.create_package(name="Pro", quota=20, price_cents=990, valid_days=30)
        cdk_result = redemption.create_cdks(name="Quota", type="quota", count=1, quota=5)
        order = billing.create_order(user_id=str(user["id"]), email=str(user["email"]), package_id=str(package["id"]))
        paid = billing.mark_paid(
            str(order["id"]),
            provider="mock",
            provider_payment_id="db-pay-1",
            amount_cents=990,
            actor={"role": "admin", "id": "admin"},
        )

        self.assertEqual(paid["order"]["status"], "fulfilled")
        self.assertEqual(paid["user"]["quota_balance"], 20)
        session = storage.Session()
        try:
            self.assertEqual(session.query(UserModel).count(), 1)
            self.assertEqual(session.query(PackageModel).count(), 1)
            self.assertEqual(session.query(CDKModel).count(), 1)
            self.assertEqual(session.query(OrderModel).count(), 1)
            self.assertEqual(session.query(PaymentModel).count(), 1)
            self.assertEqual(session.query(QuotaLedgerModel).count(), 1)
            self.assertEqual(session.query(CDKModel).first().code_prefix, cdk_result["created"][0]["code_prefix"])
        finally:
            session.close()

    def test_auth_sessions_and_action_tokens_use_dedicated_database_tables(self):
        storage = self.create_storage()
        auth = AuthService(storage)
        user, raw_key, key = auth.register_user("user@example.com", "StrongPass123")
        identity = {**key, **user, "user_id": user["id"], "key_id": key["id"]}

        _, session_token = auth.create_session(identity, ttl_seconds=600)
        _, verify_token = auth.create_email_verification_token(str(user["id"]))
        verified = auth.verify_email_token(verify_token)
        _, reset_token = auth.create_password_reset_token("user@example.com")

        self.assertTrue(verified["email_verified"])
        self.assertIsNotNone(auth.authenticate(session_token))
        self.assertIsNotNone(auth.authenticate(raw_key))

        session = storage.Session()
        try:
            self.assertEqual(session.query(AuthSessionModel).count(), 1)
            self.assertEqual(session.query(AuthActionTokenModel).count(), 2)
            self.assertEqual(session.query(CollectionItemModel).filter(CollectionItemModel.collection.in_(["auth_sessions", "auth_action_tokens"])).count(), 0)
            self.assertEqual(session.query(AuthSessionModel).first().user_id, user["id"])
            self.assertEqual(session.query(AuthActionTokenModel).filter(AuthActionTokenModel.type == "email_verify").first().email, "user@example.com")
            self.assertIsNotNone(session.query(AuthActionTokenModel).filter(AuthActionTokenModel.type == "email_verify").first().used_at)
            self.assertIsNone(session.query(AuthActionTokenModel).filter(AuthActionTokenModel.type == "password_reset").first().used_at)
        finally:
            session.close()

        reloaded = AuthService(storage)
        self.assertIsNotNone(reloaded.authenticate(session_token))
        reset_user = reloaded.reset_password_with_token(str(reset_token), "NewStrongPass123")
        self.assertEqual(reset_user["email"], "user@example.com")
        self.assertIsNone(reloaded.authenticate(session_token))
        self.assertIsNone(reloaded.authenticate(raw_key))

        session = storage.Session()
        try:
            session_row = session.query(AuthSessionModel).first()
            reset_row = session.query(AuthActionTokenModel).filter(AuthActionTokenModel.type == "password_reset").first()
            self.assertIsNotNone(session_row.revoked_at)
            self.assertIsNotNone(reset_row.used_at)
            self.assertEqual(storage.health_check()["dedicated_collection_counts"]["auth_sessions"], 1)
            self.assertEqual(storage.health_check()["dedicated_collection_counts"]["auth_action_tokens"], 2)
        finally:
            session.close()

    def test_support_tickets_use_dedicated_database_table(self):
        storage = self.create_storage()
        ticket = {
            "id": "tic_1",
            "user_id": "usr_1",
            "email": "user@example.com",
            "subject": "Need help",
            "category": "billing",
            "priority": "high",
            "status": "open",
            "created_at": "2026-07-07T00:00:00+00:00",
            "updated_at": "2026-07-07T00:01:00+00:00",
            "last_message_at": "2026-07-07T00:01:00+00:00",
            "messages": [
                {
                    "id": "msg_1",
                    "author_type": "user",
                    "author_id": "usr_1",
                    "body": "Help",
                    "created_at": "2026-07-07T00:01:00+00:00",
                }
            ],
        }

        storage.append_collection_item("support_tickets", ticket)

        self.assertEqual(storage.load_collection("support_tickets"), [ticket])
        session = storage.Session()
        try:
            self.assertEqual(session.query(SupportTicketModel).count(), 1)
            self.assertEqual(session.query(SupportTicketModel).first().priority, "high")
            self.assertEqual(session.query(CollectionItemModel).filter(CollectionItemModel.collection == "support_tickets").count(), 0)
            self.assertEqual(storage.health_check()["dedicated_collection_counts"]["support_tickets"], 1)
        finally:
            session.close()

    def test_legacy_auth_collections_are_migrated_to_dedicated_tables(self):
        storage = self.create_storage()
        legacy_session = {
            "id": "sess_legacy",
            "token_hash": "hash-session",
            "role": "user",
            "user_id": "usr_legacy",
            "email": "legacy@example.com",
            "created_at": "2026-07-07T00:00:00+00:00",
            "last_used_at": "2026-07-07T00:00:00+00:00",
            "expires_at": "2026-07-08T00:00:00+00:00",
            "revoked_at": None,
        }
        legacy_token = {
            "id": "tok_legacy",
            "type": "email_verify",
            "token_hash": "hash-token",
            "user_id": "usr_legacy",
            "email": "legacy@example.com",
            "created_at": "2026-07-07T00:00:00+00:00",
            "expires_at": "2026-07-08T00:00:00+00:00",
            "used_at": None,
        }
        session = storage.Session()
        try:
            session.add(CollectionItemModel(collection="auth_sessions", item_id="sess_legacy", data=json.dumps(legacy_session)))
            session.add(CollectionItemModel(collection="auth_action_tokens", item_id="tok_legacy", data=json.dumps(legacy_token)))
            session.commit()
        finally:
            session.close()

        self.assertEqual(storage.load_collection("auth_sessions"), [legacy_session])
        self.assertEqual(storage.load_collection("auth_action_tokens"), [legacy_token])
        session = storage.Session()
        try:
            self.assertEqual(session.query(AuthSessionModel).count(), 1)
            self.assertEqual(session.query(AuthActionTokenModel).count(), 1)
            self.assertEqual(session.query(AuthSessionModel).first().email, "legacy@example.com")
            self.assertEqual(session.query(AuthActionTokenModel).first().type, "email_verify")
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()
