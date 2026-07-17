from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Literal

from services.auth_service import AuthService, auth_service
from services.config import config
from services.redemption_service import RedemptionService, redemption_service
from services.storage.base import StorageBackend

OrderStatus = Literal["created", "pending_payment", "paid", "fulfilled", "cancelled", "refunded"]
PaymentStatus = Literal["succeeded", "refunded"]

PAYABLE_ORDER_STATUSES = {"created", "pending_payment", "paid"}
TERMINAL_ORDER_STATUSES = {"fulfilled", "cancelled", "refunded"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(value: object) -> str:
    return str(value or "").strip()


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_non_negative_int(value: object, default: int = 0) -> int:
    return max(0, _safe_int(value, default))


def _safe_positive_int(value: object, default: int = 1) -> int:
    return max(1, _safe_int(value, default))


def _currency(value: object) -> str:
    normalized = _clean(value).upper() or "CNY"
    return "".join(ch for ch in normalized if ch.isalnum())[:8] or "CNY"


def _normalize_metadata(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items() if str(key).strip()}


def _parse_datetime(value: object) -> datetime | None:
    raw = _clean(value)
    if not raw:
        return None
    try:
        candidate = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if candidate.tzinfo is None:
        candidate = candidate.replace(tzinfo=timezone.utc)
    return candidate


class BillingService:
    """Orders, payments and quota fulfillment MVP.

    The service uses the existing storage abstraction so it works with JSON,
    SQLite/PostgreSQL and Git backends. Payment confirmation is idempotent by
    `provider + provider_payment_id` or `idempotency_key`; fulfillment is also
    guarded by order status and quota-ledger references to avoid double grants.
    """

    def __init__(self, storage: StorageBackend, auth: AuthService, redemption: RedemptionService):
        self.storage = storage
        self.auth_service = auth
        self.redemption_service = redemption
        self._lock = Lock()
        self._orders = self._load_orders()
        self._payments = self._load_payments()

    @staticmethod
    def new_order_id() -> str:
        return f"ord_{uuid.uuid4().hex[:16]}"

    @staticmethod
    def new_payment_id() -> str:
        return f"pay_{uuid.uuid4().hex[:16]}"

    def _load_orders(self) -> list[dict[str, object]]:
        try:
            items = self.storage.load_collection("orders")
        except Exception:
            return []
        if not isinstance(items, list):
            return []
        orders = [normalized for item in items if (normalized := self._normalize_order(item)) is not None]
        orders.sort(key=lambda item: str(item.get("created_at") or ""))
        return orders

    def _load_payments(self) -> list[dict[str, object]]:
        try:
            items = self.storage.load_collection("payments")
        except Exception:
            return []
        if not isinstance(items, list):
            return []
        payments = [normalized for item in items if (normalized := self._normalize_payment(item)) is not None]
        payments.sort(key=lambda item: str(item.get("created_at") or ""))
        return payments

    def _save_orders(self) -> None:
        self.storage.save_collection("orders", self._orders)

    def _save_payments(self) -> None:
        self.storage.save_collection("payments", self._payments)

    def _save_order(self, order: dict[str, object]) -> None:
        self.storage.append_collection_item("orders", order)

    def _save_payment(self, payment: dict[str, object]) -> None:
        self.storage.append_collection_item("payments", payment)

    def _normalize_order(self, raw: object) -> dict[str, object] | None:
        if not isinstance(raw, dict):
            return None
        order_id = _clean(raw.get("id"))
        user_id = _clean(raw.get("user_id"))
        package_id = _clean(raw.get("package_id"))
        if not order_id or not user_id or not package_id:
            return None
        status = _clean(raw.get("status")) or "pending_payment"
        if status not in {"created", "pending_payment", "paid", "fulfilled", "cancelled", "refunded"}:
            status = "pending_payment"
        quantity = _safe_positive_int(raw.get("quantity"), 1)
        package_snapshot = raw.get("package_snapshot") if isinstance(raw.get("package_snapshot"), dict) else {}
        package_name = _clean(raw.get("package_name") or package_snapshot.get("name"))
        quota_total = _safe_non_negative_int(raw.get("quota_total"), _safe_int(package_snapshot.get("quota"), 0) * quantity)
        amount_cents = _safe_non_negative_int(
            raw.get("amount_cents"),
            _safe_int(package_snapshot.get("price_cents"), 0) * quantity,
        )
        currency = _currency(raw.get("currency") or package_snapshot.get("currency"))
        created_at = _clean(raw.get("created_at")) or _now_iso()
        updated_at = _clean(raw.get("updated_at")) or created_at
        return {
            "id": order_id,
            "user_id": user_id,
            "email": _clean(raw.get("email")) or None,
            "package_id": package_id,
            "package_name": package_name,
            "package_snapshot": dict(package_snapshot),
            "quantity": quantity,
            "quota_total": quota_total,
            "amount_cents": amount_cents,
            "currency": currency,
            "status": status,
            "payment_id": _clean(raw.get("payment_id")) or None,
            "provider": _clean(raw.get("provider")) or None,
            "provider_payment_id": _clean(raw.get("provider_payment_id")) or None,
            "idempotency_key": _clean(raw.get("idempotency_key")) or None,
            "quota_granted": _safe_non_negative_int(raw.get("quota_granted")),
            "package_expires_at": _clean(raw.get("package_expires_at")) or None,
            "created_at": created_at,
            "updated_at": updated_at,
            "paid_at": _clean(raw.get("paid_at")) or None,
            "fulfilled_at": _clean(raw.get("fulfilled_at")) or None,
            "cancelled_at": _clean(raw.get("cancelled_at")) or None,
            "refunded_at": _clean(raw.get("refunded_at")) or None,
            "metadata": _normalize_metadata(raw.get("metadata")),
        }

    def _normalize_payment(self, raw: object) -> dict[str, object] | None:
        if not isinstance(raw, dict):
            return None
        payment_id = _clean(raw.get("id"))
        order_id = _clean(raw.get("order_id"))
        user_id = _clean(raw.get("user_id"))
        if not payment_id or not order_id or not user_id:
            return None
        provider = _clean(raw.get("provider")) or "manual"
        created_at = _clean(raw.get("created_at")) or _now_iso()
        paid_at = _clean(raw.get("paid_at")) or created_at
        status = _clean(raw.get("status")) or "succeeded"
        if status not in {"succeeded", "refunded"}:
            status = "succeeded"
        return {
            "id": payment_id,
            "order_id": order_id,
            "user_id": user_id,
            "email": _clean(raw.get("email")) or None,
            "provider": provider,
            "provider_payment_id": _clean(raw.get("provider_payment_id")) or None,
            "idempotency_key": _clean(raw.get("idempotency_key")) or None,
            "amount_cents": _safe_non_negative_int(raw.get("amount_cents")),
            "currency": _currency(raw.get("currency")),
            "status": status,
            "created_at": created_at,
            "paid_at": paid_at,
            "refunded_at": _clean(raw.get("refunded_at")) or None,
            "metadata": _normalize_metadata(raw.get("metadata")),
        }

    @staticmethod
    def _public_order(item: dict[str, object]) -> dict[str, object]:
        return {
            "id": item.get("id"),
            "user_id": item.get("user_id"),
            "email": item.get("email"),
            "package_id": item.get("package_id"),
            "package_name": item.get("package_name"),
            "package_snapshot": dict(item.get("package_snapshot") or {}),
            "quantity": item.get("quantity"),
            "quota_total": item.get("quota_total"),
            "amount_cents": item.get("amount_cents"),
            "currency": item.get("currency"),
            "status": item.get("status"),
            "payment_id": item.get("payment_id"),
            "provider": item.get("provider"),
            "provider_payment_id": item.get("provider_payment_id"),
            "quota_granted": item.get("quota_granted"),
            "package_expires_at": item.get("package_expires_at"),
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
            "paid_at": item.get("paid_at"),
            "fulfilled_at": item.get("fulfilled_at"),
            "cancelled_at": item.get("cancelled_at"),
            "refunded_at": item.get("refunded_at"),
            "metadata": dict(item.get("metadata") or {}),
        }

    @staticmethod
    def _public_payment(item: dict[str, object]) -> dict[str, object]:
        return {
            "id": item.get("id"),
            "order_id": item.get("order_id"),
            "user_id": item.get("user_id"),
            "email": item.get("email"),
            "provider": item.get("provider"),
            "provider_payment_id": item.get("provider_payment_id"),
            "idempotency_key": item.get("idempotency_key"),
            "amount_cents": item.get("amount_cents"),
            "currency": item.get("currency"),
            "status": item.get("status"),
            "created_at": item.get("created_at"),
            "paid_at": item.get("paid_at"),
            "refunded_at": item.get("refunded_at"),
            "metadata": dict(item.get("metadata") or {}),
        }

    def _find_order_index(self, order_id: str) -> int | None:
        normalized_id = _clean(order_id)
        for index, order in enumerate(self._orders):
            if order.get("id") == normalized_id:
                return index
        return None

    def _find_payment_by_reference(
        self,
        *,
        provider: str,
        provider_payment_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, object] | None:
        normalized_provider = _clean(provider) or "manual"
        normalized_provider_payment_id = _clean(provider_payment_id)
        normalized_idempotency_key = _clean(idempotency_key)
        for payment in self._payments:
            if normalized_idempotency_key and payment.get("idempotency_key") == normalized_idempotency_key:
                return payment
            if (
                normalized_provider_payment_id
                and payment.get("provider") == normalized_provider
                and payment.get("provider_payment_id") == normalized_provider_payment_id
            ):
                return payment
        return None

    def _get_user(self, user_id: str) -> dict[str, object] | None:
        normalized_id = _clean(user_id)
        if not normalized_id:
            return None
        for user in self.auth_service.list_users():
            if user.get("id") == normalized_id:
                return user
        return None

    def _existing_order_grant(self, order: dict[str, object]) -> bool:
        user_id = _clean(order.get("user_id"))
        order_id = _clean(order.get("id"))
        if not user_id or not order_id:
            return False
        for item in self.auth_service.list_quota_ledger(user_id=user_id, limit=1000):
            if item.get("ref_type") == "order" and item.get("ref_id") == order_id and _safe_int(item.get("amount")) > 0:
                return True
        return False

    def _compute_package_expires_at(self, order: dict[str, object]) -> str | None:
        snapshot = order.get("package_snapshot") if isinstance(order.get("package_snapshot"), dict) else {}
        valid_days = _safe_non_negative_int(snapshot.get("valid_days"))
        if valid_days <= 0:
            return None
        now = datetime.now(timezone.utc)
        base = now
        user = self._get_user(str(order.get("user_id") or ""))
        current_expiry = _parse_datetime((user or {}).get("package_expires_at"))
        if (
            user
            and user.get("package_id") == order.get("package_id")
            and current_expiry is not None
            and current_expiry > now
        ):
            base = current_expiry
        return (base + timedelta(days=valid_days)).isoformat()

    def create_order(
        self,
        *,
        user_id: str,
        package_id: str,
        email: str | None = None,
        quantity: int = 1,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        normalized_user_id = _clean(user_id)
        if not normalized_user_id:
            raise ValueError("registered user is required")
        user = self._get_user(normalized_user_id)
        if user is None or not bool(user.get("enabled", True)):
            raise ValueError("user is invalid")
        package = self.redemption_service.get_package(package_id)
        if package is None:
            raise ValueError("package is invalid")
        quantity = _safe_positive_int(quantity, 1)
        quota = _safe_non_negative_int(package.get("quota"))
        price_cents = _safe_non_negative_int(package.get("price_cents"))
        now = _now_iso()
        order = {
            "id": self.new_order_id(),
            "user_id": normalized_user_id,
            "email": _clean(email) or user.get("email"),
            "package_id": package.get("id"),
            "package_name": package.get("name"),
            "package_snapshot": dict(package),
            "quantity": quantity,
            "quota_total": quota * quantity,
            "amount_cents": price_cents * quantity,
            "currency": _currency(package.get("currency")),
            "status": "pending_payment",
            "payment_id": None,
            "provider": None,
            "provider_payment_id": None,
            "idempotency_key": None,
            "quota_granted": 0,
            "package_expires_at": None,
            "created_at": now,
            "updated_at": now,
            "paid_at": None,
            "fulfilled_at": None,
            "cancelled_at": None,
            "refunded_at": None,
            "metadata": _normalize_metadata(metadata),
        }
        with self._lock:
            self._orders.append(order)
            self._save_order(order)
            return self._public_order(order)

    def list_orders(
        self,
        identity: dict[str, object] | None = None,
        *,
        limit: int = 100,
        status: str | None = None,
    ) -> list[dict[str, object]]:
        safe_limit = min(max(1, int(limit or 100)), 1000)
        normalized_status = _clean(status)
        with self._lock:
            items = list(self._orders)
            if normalized_status:
                items = [item for item in items if item.get("status") == normalized_status]
            if identity is not None and identity.get("role") != "admin":
                user_id = _clean(identity.get("user_id"))
                items = [item for item in items if item.get("user_id") == user_id]
            items.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
            return [self._public_order(item) for item in items[:safe_limit]]

    def get_order(self, order_id: str, identity: dict[str, object] | None = None) -> dict[str, object] | None:
        normalized_id = _clean(order_id)
        with self._lock:
            for item in self._orders:
                if item.get("id") != normalized_id:
                    continue
                if identity is not None and identity.get("role") != "admin" and item.get("user_id") != _clean(identity.get("user_id")):
                    return None
                return self._public_order(item)
        return None

    def list_payments(
        self,
        *,
        limit: int = 100,
        provider: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, object]]:
        safe_limit = min(max(1, int(limit or 100)), 1000)
        normalized_provider = _clean(provider)
        normalized_status = _clean(status)
        with self._lock:
            items = list(self._payments)
            if normalized_provider:
                items = [item for item in items if item.get("provider") == normalized_provider]
            if normalized_status:
                items = [item for item in items if item.get("status") == normalized_status]
            items.sort(key=lambda item: str(item.get("paid_at") or item.get("created_at") or ""), reverse=True)
            return [self._public_payment(item) for item in items[:safe_limit]]

    def cancel_order(
        self,
        order_id: str,
        identity: dict[str, object],
        *,
        reason: str = "",
    ) -> dict[str, object] | None:
        normalized_id = _clean(order_id)
        with self._lock:
            index = self._find_order_index(normalized_id)
            if index is None:
                return None
            order = self._orders[index]
            if identity.get("role") != "admin" and order.get("user_id") != _clean(identity.get("user_id")):
                return None
            if order.get("status") not in {"created", "pending_payment"}:
                raise ValueError("only unpaid orders can be cancelled")
            next_order = dict(order)
            now = _now_iso()
            next_order["status"] = "cancelled"
            next_order["cancelled_at"] = now
            next_order["updated_at"] = now
            metadata = dict(next_order.get("metadata") or {})
            if reason:
                metadata["cancel_reason"] = reason
            next_order["metadata"] = metadata
            self._orders[index] = next_order
            self._save_order(next_order)
            return self._public_order(next_order)

    def attach_checkout_session(
        self,
        order_id: str,
        identity: dict[str, object],
        checkout: dict[str, object],
    ) -> dict[str, object] | None:
        """Attach the latest checkout attempt to an unpaid order metadata."""

        normalized_id = _clean(order_id)
        with self._lock:
            index = self._find_order_index(normalized_id)
            if index is None:
                return None
            order = dict(self._orders[index])
            if identity.get("role") != "admin" and order.get("user_id") != _clean(identity.get("user_id")):
                return None
            if order.get("status") not in {"created", "pending_payment"}:
                raise ValueError("only unpaid orders can create checkout")
            metadata = dict(order.get("metadata") or {})
            safe_checkout = _normalize_metadata(checkout)
            history = metadata.get("checkout_history")
            if not isinstance(history, list):
                history = []
            history.append({
                "id": safe_checkout.get("id"),
                "provider": safe_checkout.get("provider"),
                "mode": safe_checkout.get("mode"),
                "created_at": safe_checkout.get("created_at"),
                "provider_session_id": safe_checkout.get("provider_session_id"),
            })
            metadata["checkout"] = safe_checkout
            metadata["checkout_history"] = history[-10:]
            now = _now_iso()
            order["metadata"] = metadata
            order["updated_at"] = now
            provider = _clean(safe_checkout.get("provider"))
            if provider:
                order["provider"] = provider
            self._orders[index] = order
            self._save_order(order)
            return self._public_order(order)

    def mark_paid(
        self,
        order_id: str,
        *,
        provider: str = "manual",
        provider_payment_id: str | None = None,
        amount_cents: int | None = None,
        currency: str | None = None,
        idempotency_key: str | None = None,
        actor: dict[str, object] | None = None,
        metadata: dict[str, object] | None = None,
        auto_fulfill: bool = True,
    ) -> dict[str, object]:
        normalized_order_id = _clean(order_id)
        normalized_provider = _clean(provider) or "manual"
        normalized_idempotency_key = _clean(idempotency_key) or f"{normalized_provider}:{normalized_order_id}"
        normalized_provider_payment_id = _clean(provider_payment_id) or normalized_idempotency_key

        with self._lock:
            index = self._find_order_index(normalized_order_id)
            if index is None:
                raise ValueError("order not found")
            order = dict(self._orders[index])
            if order.get("status") in {"cancelled", "refunded"}:
                raise ValueError("order cannot be paid")

            existing_payment = self._find_payment_by_reference(
                provider=normalized_provider,
                provider_payment_id=normalized_provider_payment_id,
                idempotency_key=normalized_idempotency_key,
            )
            if existing_payment is not None:
                if existing_payment.get("order_id") != normalized_order_id:
                    raise ValueError("payment idempotency conflict")
                if order.get("status") not in {"paid", "fulfilled"}:
                    order.update({
                        "status": "paid",
                        "payment_id": existing_payment.get("id"),
                        "provider": existing_payment.get("provider"),
                        "provider_payment_id": existing_payment.get("provider_payment_id"),
                        "idempotency_key": existing_payment.get("idempotency_key"),
                        "paid_at": existing_payment.get("paid_at"),
                        "updated_at": _now_iso(),
                    })
                    self._orders[index] = order
                    self._save_order(order)
                if auto_fulfill:
                    return self._fulfill_order_locked(normalized_order_id, actor=actor)
                return {"order": self._public_order(order), "payment": self._public_payment(existing_payment), "idempotent": True}

            if order.get("status") == "fulfilled":
                raise ValueError("order already fulfilled")
            if order.get("status") not in PAYABLE_ORDER_STATUSES:
                raise ValueError("order cannot be paid")

            paid_at = _now_iso()
            payment = {
                "id": self.new_payment_id(),
                "order_id": normalized_order_id,
                "user_id": order.get("user_id"),
                "email": order.get("email"),
                "provider": normalized_provider,
                "provider_payment_id": normalized_provider_payment_id,
                "idempotency_key": normalized_idempotency_key,
                "amount_cents": _safe_non_negative_int(amount_cents, _safe_non_negative_int(order.get("amount_cents"))),
                "currency": _currency(currency or order.get("currency")),
                "status": "succeeded",
                "created_at": paid_at,
                "paid_at": paid_at,
                "refunded_at": None,
                "metadata": _normalize_metadata(metadata),
            }
            order.update({
                "status": "paid",
                "payment_id": payment.get("id"),
                "provider": payment.get("provider"),
                "provider_payment_id": payment.get("provider_payment_id"),
                "idempotency_key": payment.get("idempotency_key"),
                "paid_at": paid_at,
                "updated_at": paid_at,
            })
            self._payments.append(payment)
            self._orders[index] = order
            self._save_payment(payment)
            self._save_order(order)
            if auto_fulfill:
                return self._fulfill_order_locked(normalized_order_id, actor=actor)
            return {"order": self._public_order(order), "payment": self._public_payment(payment), "idempotent": False}

    def fulfill_order(self, order_id: str, *, actor: dict[str, object] | None = None) -> dict[str, object]:
        with self._lock:
            return self._fulfill_order_locked(order_id, actor=actor)

    def _fulfill_order_locked(self, order_id: str, *, actor: dict[str, object] | None = None) -> dict[str, object]:
        index = self._find_order_index(order_id)
        if index is None:
            raise ValueError("order not found")
        order = dict(self._orders[index])
        if order.get("status") == "fulfilled":
            payment = self._payment_for_order(order)
            return {"order": self._public_order(order), "payment": self._public_payment(payment) if payment else None, "idempotent": True}
        if order.get("status") != "paid":
            raise ValueError("order is not paid")

        quota_total = _safe_non_negative_int(order.get("quota_total"))
        should_grant = quota_total > 0 and not self._existing_order_grant(order)
        user = None
        if should_grant:
            user = self.auth_service.adjust_user_quota(
                str(order.get("user_id") or ""),
                quota_total,
                "order",
                ref_type="order",
                ref_id=str(order.get("id") or ""),
                actor_type=str((actor or {}).get("role") or "system"),
                actor_id=str((actor or {}).get("user_id") or (actor or {}).get("id") or "system"),
                metadata={
                    "package_id": order.get("package_id"),
                    "package_name": order.get("package_name"),
                    "quantity": order.get("quantity"),
                    "payment_id": order.get("payment_id"),
                    "amount_cents": order.get("amount_cents"),
                    "currency": order.get("currency"),
                },
            )
            if user is None:
                raise ValueError("user is invalid")
        elif quota_total > 0:
            user = self._get_user(str(order.get("user_id") or ""))

        package_expires_at = self._compute_package_expires_at(order)
        package_update = {
            "package_id": order.get("package_id"),
            "package_name": order.get("package_name"),
            "package_expires_at": package_expires_at,
        }
        updated_user = self.auth_service.update_user(str(order.get("user_id") or ""), package_update)
        if updated_user is not None:
            user = updated_user

        now = _now_iso()
        order.update({
            "status": "fulfilled",
            "quota_granted": quota_total,
            "package_expires_at": package_expires_at,
            "fulfilled_at": now,
            "updated_at": now,
        })
        self._orders[index] = order
        self._save_order(order)
        payment = self._payment_for_order(order)
        return {
            "order": self._public_order(order),
            "payment": self._public_payment(payment) if payment else None,
            "user": user,
            "idempotent": not should_grant,
        }

    def _payment_for_order(self, order: dict[str, object]) -> dict[str, object] | None:
        payment_id = _clean(order.get("payment_id"))
        for payment in self._payments:
            if payment_id and payment.get("id") == payment_id:
                return payment
        order_id = _clean(order.get("id"))
        for payment in self._payments:
            if payment.get("order_id") == order_id:
                return payment
        return None

    def _find_payment_index_for_order(self, order: dict[str, object]) -> int | None:
        payment_id = _clean(order.get("payment_id"))
        for index, payment in enumerate(self._payments):
            if payment_id and payment.get("id") == payment_id:
                return index
        order_id = _clean(order.get("id"))
        for index, payment in enumerate(self._payments):
            if payment.get("order_id") == order_id:
                return index
        return None

    def refund_order(
        self,
        order_id: str,
        *,
        actor: dict[str, object] | None = None,
        reason: str = "",
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Refund a fulfilled order and claw back still-available quota.

        The first commercial refund implementation is intentionally strict:
        fulfilled orders can be refunded only when the user's current balance
        can cover the quota granted by the order. This avoids silently creating
        negative balances or partially refunded orders. Repeated refund calls
        for an already-refunded order are idempotent and do not write another
        quota-ledger row.
        """

        normalized_order_id = _clean(order_id)
        if not normalized_order_id:
            raise ValueError("order not found")

        with self._lock:
            index = self._find_order_index(normalized_order_id)
            if index is None:
                raise ValueError("order not found")
            order = dict(self._orders[index])
            payment_index = self._find_payment_index_for_order(order)
            payment = dict(self._payments[payment_index]) if payment_index is not None else None

            if order.get("status") == "refunded":
                user = self._get_user(str(order.get("user_id") or ""))
                return {
                    "order": self._public_order(order),
                    "payment": self._public_payment(payment) if payment else None,
                    "user": user,
                    "idempotent": True,
                }
            if order.get("status") != "fulfilled":
                raise ValueError("only fulfilled orders can be refunded")
            if payment is None:
                raise ValueError("payment not found")
            if payment.get("status") == "refunded":
                raise ValueError("payment is already refunded but order is not marked refunded")

            quota_to_deduct = _safe_non_negative_int(order.get("quota_granted"))
            user = self._get_user(str(order.get("user_id") or ""))
            if user is None:
                raise ValueError("user is invalid")
            current_balance = _safe_non_negative_int(user.get("quota_balance"))
            if quota_to_deduct > current_balance:
                raise ValueError("user quota balance is insufficient for refund")

            actor_type = str((actor or {}).get("role") or "system")
            actor_id = str((actor or {}).get("user_id") or (actor or {}).get("id") or "system")
            normalized_reason = _clean(reason) or "admin-refund"
            refund_metadata = {
                "order_id": order.get("id"),
                "payment_id": payment.get("id"),
                "package_id": order.get("package_id"),
                "package_name": order.get("package_name"),
                "quantity": order.get("quantity"),
                "amount_cents": order.get("amount_cents"),
                "currency": order.get("currency"),
                "reason": normalized_reason,
                **_normalize_metadata(metadata),
            }

            if quota_to_deduct:
                user = self.auth_service.adjust_user_quota(
                    str(order.get("user_id") or ""),
                    -quota_to_deduct,
                    "refund",
                    ref_type="order_refund",
                    ref_id=str(order.get("id") or ""),
                    actor_type=actor_type,
                    actor_id=actor_id,
                    metadata=refund_metadata,
                )
                if user is None:
                    raise ValueError("user is invalid")

            now = _now_iso()
            order_metadata = dict(order.get("metadata") or {})
            order_metadata.update({
                "refund_reason": normalized_reason,
                "refund_actor_type": actor_type,
                "refund_actor_id": actor_id,
                "refund_payment_id": payment.get("id"),
                "refund_quota_deducted": quota_to_deduct,
                "refund_metadata": _normalize_metadata(metadata),
            })
            order.update({
                "status": "refunded",
                "refunded_at": now,
                "updated_at": now,
                "metadata": order_metadata,
            })

            payment_metadata = dict(payment.get("metadata") or {})
            payment_metadata.update({
                "refund_reason": normalized_reason,
                "refund_actor_type": actor_type,
                "refund_actor_id": actor_id,
                "refund_order_id": order.get("id"),
                "refund_quota_deducted": quota_to_deduct,
                "refund_metadata": _normalize_metadata(metadata),
            })
            payment.update({
                "status": "refunded",
                "refunded_at": now,
                "metadata": payment_metadata,
            })

            self._orders[index] = order
            if payment_index is not None:
                self._payments[payment_index] = payment
            self._save_payment(payment)
            self._save_order(order)
            return {
                "order": self._public_order(order),
                "payment": self._public_payment(payment),
                "user": user,
                "quota_deducted": quota_to_deduct,
                "idempotent": False,
            }


billing_service = BillingService(config.get_storage_backend(), auth_service, redemption_service)
