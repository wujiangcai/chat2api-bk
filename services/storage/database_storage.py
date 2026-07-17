from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Column, Integer, String, Text, UniqueConstraint, create_engine, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from services.storage.base import StorageBackend
from services.storage.migrations import get_applied_migrations, run_migrations

Base = declarative_base()


def _clean(value: object) -> str:
    return str(value or "").strip()


def _none_if_blank(value: object) -> str | None:
    cleaned = _clean(value)
    return cleaned or None


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_non_negative_int(value: object, default: int = 0) -> int:
    return max(0, _safe_int(value, default))


def _bool_int(value: object, default: bool = True) -> int:
    if isinstance(value, str):
        return 1 if value.strip().lower() not in {"0", "false", "no", "off"} else 0
    if value is None:
        return 1 if default else 0
    return 1 if bool(value) else 0


def _json_data(item: dict[str, Any]) -> str:
    return json.dumps(item, ensure_ascii=False)


def _json_load(value: object) -> dict[str, Any]:
    try:
        data = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _collection_item_id(item: dict[str, Any]) -> str:
    return _clean(item.get("id"))


class AccountModel(Base):
    """Provider account pool record."""
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    access_token = Column(String(2048), unique=True, nullable=False, index=True)
    data = Column(Text, nullable=False)


class AuthKeyModel(Base):
    """API/auth key record."""
    __tablename__ = "auth_keys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key_id = Column(String(255), unique=True, nullable=False, index=True)
    data = Column(Text, nullable=False)


class CollectionItemModel(Base):
    """Legacy generic collection item.

    New commercial collections are stored in dedicated tables below. This table
    remains for backwards compatibility and for non-commercial extension data.
    """
    __tablename__ = "storage_collections"
    __table_args__ = (UniqueConstraint("collection", "item_id", name="uq_storage_collection_item"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    collection = Column(String(255), nullable=False, index=True)
    item_id = Column(String(255), nullable=False, index=True)
    data = Column(Text, nullable=False)


class UserModel(Base):
    __tablename__ = "users"

    id = Column(String(255), primary_key=True)
    email = Column(String(320), nullable=True, index=True)
    role = Column(String(64), nullable=True, index=True)
    enabled = Column(Integer, nullable=False, default=1, index=True)
    quota_balance = Column(Integer, nullable=False, default=0)
    package_id = Column(String(255), nullable=True, index=True)
    created_at = Column(String(64), nullable=True, index=True)
    updated_at = Column(String(64), nullable=True, index=True)
    data = Column(Text, nullable=False)


class PackageModel(Base):
    __tablename__ = "packages"

    id = Column(String(255), primary_key=True)
    name = Column(String(255), nullable=True)
    enabled = Column(Integer, nullable=False, default=1, index=True)
    quota = Column(Integer, nullable=False, default=0)
    price_cents = Column(Integer, nullable=False, default=0)
    currency = Column(String(16), nullable=True, index=True)
    created_at = Column(String(64), nullable=True, index=True)
    updated_at = Column(String(64), nullable=True, index=True)
    data = Column(Text, nullable=False)


class CDKModel(Base):
    __tablename__ = "cdks"

    id = Column(String(255), primary_key=True)
    code_hash = Column(String(255), nullable=True, unique=True, index=True)
    code_prefix = Column(String(32), nullable=True, index=True)
    name = Column(String(255), nullable=True)
    type = Column(String(64), nullable=True, index=True)
    package_id = Column(String(255), nullable=True, index=True)
    enabled = Column(Integer, nullable=False, default=1, index=True)
    redeemed_count = Column(Integer, nullable=False, default=0)
    expires_at = Column(String(64), nullable=True, index=True)
    created_at = Column(String(64), nullable=True, index=True)
    updated_at = Column(String(64), nullable=True, index=True)
    data = Column(Text, nullable=False)


class RedemptionModel(Base):
    __tablename__ = "redemptions"

    id = Column(String(255), primary_key=True)
    cdk_id = Column(String(255), nullable=True, index=True)
    user_id = Column(String(255), nullable=True, index=True)
    email = Column(String(320), nullable=True, index=True)
    type = Column(String(64), nullable=True, index=True)
    quota_granted = Column(Integer, nullable=False, default=0)
    redeemed_at = Column(String(64), nullable=True, index=True)
    data = Column(Text, nullable=False)


class OrderModel(Base):
    __tablename__ = "orders"

    id = Column(String(255), primary_key=True)
    user_id = Column(String(255), nullable=False, index=True)
    email = Column(String(320), nullable=True, index=True)
    package_id = Column(String(255), nullable=True, index=True)
    status = Column(String(64), nullable=False, index=True)
    amount_cents = Column(Integer, nullable=False, default=0)
    currency = Column(String(16), nullable=True, index=True)
    payment_id = Column(String(255), nullable=True, index=True)
    provider = Column(String(64), nullable=True, index=True)
    provider_payment_id = Column(String(255), nullable=True, index=True)
    created_at = Column(String(64), nullable=True, index=True)
    updated_at = Column(String(64), nullable=True, index=True)
    paid_at = Column(String(64), nullable=True, index=True)
    fulfilled_at = Column(String(64), nullable=True, index=True)
    data = Column(Text, nullable=False)


class PaymentModel(Base):
    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("provider", "provider_payment_id", name="uq_payment_provider_payment_id"),
        UniqueConstraint("idempotency_key", name="uq_payment_idempotency_key"),
    )

    id = Column(String(255), primary_key=True)
    order_id = Column(String(255), nullable=False, index=True)
    user_id = Column(String(255), nullable=False, index=True)
    email = Column(String(320), nullable=True, index=True)
    provider = Column(String(64), nullable=False, index=True)
    provider_payment_id = Column(String(255), nullable=True, index=True)
    idempotency_key = Column(String(255), nullable=True, index=True)
    amount_cents = Column(Integer, nullable=False, default=0)
    currency = Column(String(16), nullable=True, index=True)
    status = Column(String(64), nullable=False, index=True)
    created_at = Column(String(64), nullable=True, index=True)
    paid_at = Column(String(64), nullable=True, index=True)
    data = Column(Text, nullable=False)


class ImageJobModel(Base):
    __tablename__ = "image_jobs"

    id = Column(String(255), primary_key=True)
    type = Column(String(64), nullable=True, index=True)
    status = Column(String(64), nullable=False, index=True)
    owner_user_id = Column(String(255), nullable=True, index=True)
    owner_key_id = Column(String(255), nullable=True, index=True)
    reserved_quota = Column(Integer, nullable=False, default=0)
    refunded_quota = Column(Integer, nullable=False, default=0)
    cost_units = Column(Integer, nullable=False, default=0)
    created_at = Column(String(64), nullable=True, index=True)
    updated_at = Column(String(64), nullable=True, index=True)
    completed_at = Column(String(64), nullable=True, index=True)
    data = Column(Text, nullable=False)


class ImageAssetModel(Base):
    __tablename__ = "image_assets"

    id = Column(String(255), primary_key=True)
    owner_user_id = Column(String(255), nullable=True, index=True)
    owner_key_id = Column(String(255), nullable=True, index=True)
    job_id = Column(String(255), nullable=True, index=True)
    source = Column(String(128), nullable=True, index=True)
    model = Column(String(128), nullable=True, index=True)
    prompt_hash = Column(String(128), nullable=True, index=True)
    object_key = Column(String(1024), nullable=True, index=True)
    status = Column(String(64), nullable=False, default="active", index=True)
    storage_backend = Column(String(64), nullable=True, index=True)
    size_bytes = Column(Integer, nullable=False, default=0)
    created_at = Column(String(64), nullable=True, index=True)
    deleted_at = Column(String(64), nullable=True, index=True)
    data = Column(Text, nullable=False)


class QuotaLedgerModel(Base):
    """Append-only quota ledger rows with queryable commercial/audit columns."""
    __tablename__ = "quota_ledger"

    id = Column(String(255), primary_key=True)
    user_id = Column(String(255), nullable=False, index=True)
    type = Column(String(64), nullable=False, index=True)
    amount = Column(Integer, nullable=False, default=0)
    balance_before = Column(Integer, nullable=False, default=0)
    balance_after = Column(Integer, nullable=False, default=0)
    reason = Column(String(255), nullable=True)
    ref_type = Column(String(64), nullable=True, index=True)
    ref_id = Column(String(255), nullable=True, index=True)
    actor_type = Column(String(64), nullable=True)
    actor_id = Column(String(255), nullable=True)
    created_at = Column(String(64), nullable=False, index=True)
    data = Column(Text, nullable=False)


class AuditLogModel(Base):
    """Persistent admin/audit log rows with queryable security columns."""
    __tablename__ = "audit_logs"

    id = Column(String(255), primary_key=True)
    action = Column(String(128), nullable=False, index=True)
    status = Column(String(64), nullable=False, default="succeeded", index=True)
    actor_type = Column(String(64), nullable=True, index=True)
    actor_id = Column(String(255), nullable=True, index=True)
    actor_email = Column(String(320), nullable=True, index=True)
    target_type = Column(String(128), nullable=True, index=True)
    target_id = Column(String(255), nullable=True, index=True)
    ip = Column(String(128), nullable=True, index=True)
    created_at = Column(String(64), nullable=False, index=True)
    data = Column(Text, nullable=False)


class LaunchEvidenceModel(Base):
    """Production launch verification evidence reports."""
    __tablename__ = "launch_evidence"

    id = Column(String(255), primary_key=True)
    name = Column(String(255), nullable=True)
    status = Column(String(64), nullable=False, index=True)
    ready = Column(Integer, nullable=False, default=0, index=True)
    source = Column(String(64), nullable=True, index=True)
    generated_at = Column(String(64), nullable=True, index=True)
    created_at = Column(String(64), nullable=False, index=True)
    created_by = Column(String(255), nullable=True, index=True)
    data = Column(Text, nullable=False)


class SupportTicketModel(Base):
    """Customer support tickets and message history for commercial operations."""
    __tablename__ = "support_tickets"

    id = Column(String(255), primary_key=True)
    user_id = Column(String(255), nullable=False, index=True)
    email = Column(String(320), nullable=True, index=True)
    status = Column(String(64), nullable=False, index=True)
    priority = Column(String(64), nullable=False, index=True)
    category = Column(String(64), nullable=True, index=True)
    assignee_id = Column(String(255), nullable=True, index=True)
    created_at = Column(String(64), nullable=False, index=True)
    updated_at = Column(String(64), nullable=False, index=True)
    last_message_at = Column(String(64), nullable=True, index=True)
    first_response_due_at = Column(String(64), nullable=True, index=True)
    first_response_at = Column(String(64), nullable=True, index=True)
    resolution_due_at = Column(String(64), nullable=True, index=True)
    resolved_at = Column(String(64), nullable=True, index=True)
    closed_at = Column(String(64), nullable=True, index=True)
    data = Column(Text, nullable=False)


class AuthSessionModel(Base):
    """HttpOnly cookie/session rows with queryable account/security columns."""
    __tablename__ = "auth_sessions"

    id = Column(String(255), primary_key=True)
    token_hash = Column(String(255), nullable=False, unique=True, index=True)
    role = Column(String(64), nullable=False, index=True)
    key_id = Column(String(255), nullable=True, index=True)
    user_id = Column(String(255), nullable=True, index=True)
    email = Column(String(320), nullable=True, index=True)
    created_at = Column(String(64), nullable=False, index=True)
    last_used_at = Column(String(64), nullable=True, index=True)
    expires_at = Column(String(64), nullable=True, index=True)
    revoked_at = Column(String(64), nullable=True, index=True)
    data = Column(Text, nullable=False)


class AuthActionTokenModel(Base):
    """Email verification/password reset action tokens."""
    __tablename__ = "auth_action_tokens"

    id = Column(String(255), primary_key=True)
    type = Column(String(64), nullable=False, index=True)
    token_hash = Column(String(255), nullable=False, unique=True, index=True)
    user_id = Column(String(255), nullable=False, index=True)
    email = Column(String(320), nullable=True, index=True)
    created_at = Column(String(64), nullable=False, index=True)
    expires_at = Column(String(64), nullable=True, index=True)
    used_at = Column(String(64), nullable=True, index=True)
    data = Column(Text, nullable=False)


DEDICATED_COLLECTION_MODELS: dict[str, type[Any]] = {
    "users": UserModel,
    "packages": PackageModel,
    "cdks": CDKModel,
    "redemptions": RedemptionModel,
    "orders": OrderModel,
    "payments": PaymentModel,
    "image_jobs": ImageJobModel,
    "image_assets": ImageAssetModel,
    "audit_logs": AuditLogModel,
    "launch_evidence": LaunchEvidenceModel,
    "support_tickets": SupportTicketModel,
    "auth_sessions": AuthSessionModel,
    "auth_action_tokens": AuthActionTokenModel,
}


class DatabaseStorageBackend(StorageBackend):
    """SQL storage backend for SQLite/PostgreSQL/MySQL compatible URLs."""

    def __init__(self, database_url: str):
        self.database_url = database_url
        self.engine = create_engine(
            database_url,
            pool_pre_ping=True,
            pool_recycle=3600,
        )
        Base.metadata.create_all(self.engine)
        self.migration_result = run_migrations(
            self.engine,
            schema_initializer=lambda engine: Base.metadata.create_all(engine),
        )
        self.Session = sessionmaker(bind=self.engine)

    def load_accounts(self) -> list[dict[str, Any]]:
        session = self.Session()
        try:
            accounts = []
            for row in session.query(AccountModel).all():
                account_data = _json_load(row.data)
                if account_data:
                    accounts.append(account_data)
            return accounts
        finally:
            session.close()

    def save_accounts(self, accounts: list[dict[str, Any]]) -> None:
        self._save_rows(AccountModel, accounts, "access_token")

    def load_auth_keys(self) -> list[dict[str, Any]]:
        return self._load_rows(AuthKeyModel)

    def save_auth_keys(self, auth_keys: list[dict[str, Any]]) -> None:
        self._save_rows(AuthKeyModel, auth_keys, "id", "key_id")

    def load_collection(self, name: str) -> list[dict[str, Any]]:
        normalized_name = str(name or "").strip()
        if not normalized_name:
            raise ValueError("collection name is required")
        if normalized_name == "quota_ledger":
            items = self._load_quota_ledger_rows()
            if items:
                return items
            legacy_items = self._load_generic_collection(normalized_name)
            if legacy_items:
                self._save_quota_ledger_rows(legacy_items)
            return legacy_items
        if normalized_name in DEDICATED_COLLECTION_MODELS:
            items = self._load_dedicated_collection(normalized_name)
            if items:
                return items
            legacy_items = self._load_generic_collection(normalized_name)
            if legacy_items:
                self._save_dedicated_rows(normalized_name, legacy_items)
            return legacy_items
        return self._load_generic_collection(normalized_name)

    def save_collection(self, name: str, items: list[dict[str, Any]]) -> None:
        normalized_name = str(name or "").strip()
        if not normalized_name:
            raise ValueError("collection name is required")
        if normalized_name == "quota_ledger":
            self._save_quota_ledger_rows(items)
            return
        if normalized_name in DEDICATED_COLLECTION_MODELS:
            self._replace_dedicated_collection(normalized_name, items)
            return
        self._replace_generic_collection(normalized_name, items)

    def append_collection_item(self, name: str, item: dict[str, Any]) -> None:
        normalized_name = str(name or "").strip()
        if not normalized_name:
            raise ValueError("collection name is required")
        if normalized_name == "quota_ledger":
            self._save_quota_ledger_rows([item])
            return
        if normalized_name in DEDICATED_COLLECTION_MODELS:
            self._save_dedicated_rows(normalized_name, [item])
            return
        super().append_collection_item(normalized_name, item)

    def _load_generic_collection(self, normalized_name: str) -> list[dict[str, Any]]:
        session = self.Session()
        try:
            items = []
            for row in session.query(CollectionItemModel).filter(CollectionItemModel.collection == normalized_name).all():
                item_data = _json_load(row.data)
                if item_data:
                    items.append(item_data)
            return items
        finally:
            session.close()

    def _replace_generic_collection(self, normalized_name: str, items: list[dict[str, Any]]) -> None:
        session = self.Session()
        try:
            session.query(CollectionItemModel).filter(CollectionItemModel.collection == normalized_name).delete()
            for item in items:
                if not isinstance(item, dict):
                    continue
                item_id = _collection_item_id(item)
                if not item_id:
                    continue
                session.add(CollectionItemModel(collection=normalized_name, item_id=item_id, data=_json_data(item)))
            session.commit()
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()

    def _load_dedicated_collection(self, normalized_name: str) -> list[dict[str, Any]]:
        model = DEDICATED_COLLECTION_MODELS[normalized_name]
        session = self.Session()
        try:
            items = []
            for row in session.query(model).order_by(model.created_at.asc()).all():
                item_data = _json_load(row.data)
                item_data.setdefault("id", row.id)
                items.append(item_data)
            return items
        finally:
            session.close()

    def _replace_dedicated_collection(self, normalized_name: str, items: list[dict[str, Any]]) -> None:
        model = DEDICATED_COLLECTION_MODELS[normalized_name]
        session = self.Session()
        try:
            session.query(model).delete()
            for item in items:
                row = self._dedicated_row(normalized_name, item)
                if row is not None:
                    session.add(row)
            session.commit()
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()

    def _save_dedicated_rows(self, normalized_name: str, items: list[dict[str, Any]]) -> None:
        session = self.Session()
        try:
            for item in items:
                row = self._dedicated_row(normalized_name, item)
                if row is not None:
                    session.merge(row)
            session.commit()
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()

    def _dedicated_row(self, normalized_name: str, item: dict[str, Any]) -> Any | None:
        if not isinstance(item, dict):
            return None
        item_id = _collection_item_id(item)
        if not item_id:
            return None
        data = _json_data(item)
        if normalized_name == "users":
            return UserModel(
                id=item_id,
                email=_none_if_blank(item.get("email")),
                role=_none_if_blank(item.get("role")),
                enabled=_bool_int(item.get("enabled", True)),
                quota_balance=_safe_non_negative_int(item.get("quota_balance")),
                package_id=_none_if_blank(item.get("package_id")),
                created_at=_none_if_blank(item.get("created_at")),
                updated_at=_none_if_blank(item.get("updated_at")),
                data=data,
            )
        if normalized_name == "packages":
            return PackageModel(
                id=item_id,
                name=_none_if_blank(item.get("name")),
                enabled=_bool_int(item.get("enabled", True)),
                quota=_safe_non_negative_int(item.get("quota")),
                price_cents=_safe_non_negative_int(item.get("price_cents")),
                currency=_none_if_blank(item.get("currency")),
                created_at=_none_if_blank(item.get("created_at")),
                updated_at=_none_if_blank(item.get("updated_at")),
                data=data,
            )
        if normalized_name == "cdks":
            return CDKModel(
                id=item_id,
                code_hash=_none_if_blank(item.get("code_hash")),
                code_prefix=_none_if_blank(item.get("code_prefix")),
                name=_none_if_blank(item.get("name")),
                type=_none_if_blank(item.get("type")),
                package_id=_none_if_blank(item.get("package_id")),
                enabled=_bool_int(item.get("enabled", True)),
                redeemed_count=_safe_non_negative_int(item.get("redeemed_count")),
                expires_at=_none_if_blank(item.get("expires_at")),
                created_at=_none_if_blank(item.get("created_at")),
                updated_at=_none_if_blank(item.get("updated_at")),
                data=data,
            )
        if normalized_name == "redemptions":
            return RedemptionModel(
                id=item_id,
                cdk_id=_none_if_blank(item.get("cdk_id")),
                user_id=_none_if_blank(item.get("user_id")),
                email=_none_if_blank(item.get("email")),
                type=_none_if_blank(item.get("type")),
                quota_granted=_safe_non_negative_int(item.get("quota_granted")),
                redeemed_at=_none_if_blank(item.get("redeemed_at")),
                data=data,
            )
        if normalized_name == "orders":
            return OrderModel(
                id=item_id,
                user_id=_clean(item.get("user_id")),
                email=_none_if_blank(item.get("email")),
                package_id=_none_if_blank(item.get("package_id")),
                status=_clean(item.get("status")) or "pending_payment",
                amount_cents=_safe_non_negative_int(item.get("amount_cents")),
                currency=_none_if_blank(item.get("currency")),
                payment_id=_none_if_blank(item.get("payment_id")),
                provider=_none_if_blank(item.get("provider")),
                provider_payment_id=_none_if_blank(item.get("provider_payment_id")),
                created_at=_none_if_blank(item.get("created_at")),
                updated_at=_none_if_blank(item.get("updated_at")),
                paid_at=_none_if_blank(item.get("paid_at")),
                fulfilled_at=_none_if_blank(item.get("fulfilled_at")),
                data=data,
            )
        if normalized_name == "payments":
            return PaymentModel(
                id=item_id,
                order_id=_clean(item.get("order_id")),
                user_id=_clean(item.get("user_id")),
                email=_none_if_blank(item.get("email")),
                provider=_clean(item.get("provider")) or "manual",
                provider_payment_id=_none_if_blank(item.get("provider_payment_id")),
                idempotency_key=_none_if_blank(item.get("idempotency_key")),
                amount_cents=_safe_non_negative_int(item.get("amount_cents")),
                currency=_none_if_blank(item.get("currency")),
                status=_clean(item.get("status")) or "succeeded",
                created_at=_none_if_blank(item.get("created_at")),
                paid_at=_none_if_blank(item.get("paid_at")),
                data=data,
            )
        if normalized_name == "image_jobs":
            owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
            return ImageJobModel(
                id=item_id,
                type=_none_if_blank(item.get("type")),
                status=_clean(item.get("status")) or "queued",
                owner_user_id=_none_if_blank(owner.get("user_id")),
                owner_key_id=_none_if_blank(owner.get("key_id")),
                reserved_quota=_safe_non_negative_int(item.get("reserved_quota")),
                refunded_quota=_safe_non_negative_int(item.get("refunded_quota")),
                cost_units=_safe_non_negative_int(item.get("cost_units")),
                created_at=_none_if_blank(item.get("created_at")),
                updated_at=_none_if_blank(item.get("updated_at")),
                completed_at=_none_if_blank(item.get("completed_at")),
                data=data,
            )
        if normalized_name == "image_assets":
            owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
            return ImageAssetModel(
                id=item_id,
                owner_user_id=_none_if_blank(owner.get("user_id")),
                owner_key_id=_none_if_blank(owner.get("key_id")),
                job_id=_none_if_blank(item.get("job_id")),
                source=_none_if_blank(item.get("source")),
                model=_none_if_blank(item.get("model")),
                prompt_hash=_none_if_blank(item.get("prompt_hash")),
                object_key=_none_if_blank(item.get("object_key")),
                status=_clean(item.get("status")) or "active",
                storage_backend=_none_if_blank(item.get("storage_backend")),
                size_bytes=_safe_non_negative_int(item.get("size_bytes")),
                created_at=_none_if_blank(item.get("created_at")),
                deleted_at=_none_if_blank(item.get("deleted_at")),
                data=data,
            )
        if normalized_name == "audit_logs":
            actor = item.get("actor") if isinstance(item.get("actor"), dict) else {}
            target = item.get("target") if isinstance(item.get("target"), dict) else {}
            request = item.get("request") if isinstance(item.get("request"), dict) else {}
            return AuditLogModel(
                id=item_id,
                action=_clean(item.get("action")) or "unknown",
                status=_clean(item.get("status")) or "succeeded",
                actor_type=_none_if_blank(actor.get("type") or actor.get("role") or item.get("actor_type")),
                actor_id=_none_if_blank(actor.get("id") or item.get("actor_id")),
                actor_email=_none_if_blank(actor.get("email") or item.get("actor_email")),
                target_type=_none_if_blank(target.get("type") or item.get("target_type")),
                target_id=_none_if_blank(target.get("id") or item.get("target_id")),
                ip=_none_if_blank(request.get("ip") or item.get("ip")),
                created_at=_clean(item.get("created_at")),
                data=data,
            )
        if normalized_name == "launch_evidence":
            return LaunchEvidenceModel(
                id=item_id,
                name=_none_if_blank(item.get("name")),
                status=_clean(item.get("status")) or "unknown",
                ready=_bool_int(item.get("ready"), default=False),
                source=_none_if_blank(item.get("source")),
                generated_at=_none_if_blank(item.get("generated_at")),
                created_at=_clean(item.get("created_at")),
                created_by=_none_if_blank(item.get("created_by")),
                data=data,
            )
        if normalized_name == "support_tickets":
            return SupportTicketModel(
                id=item_id,
                user_id=_clean(item.get("user_id")),
                email=_none_if_blank(item.get("email")),
                status=_clean(item.get("status")) or "open",
                priority=_clean(item.get("priority")) or "normal",
                category=_none_if_blank(item.get("category")),
                assignee_id=_none_if_blank(item.get("assignee_id")),
                created_at=_clean(item.get("created_at")),
                updated_at=_clean(item.get("updated_at")),
                last_message_at=_none_if_blank(item.get("last_message_at")),
                first_response_due_at=_none_if_blank(item.get("first_response_due_at")),
                first_response_at=_none_if_blank(item.get("first_response_at")),
                resolution_due_at=_none_if_blank(item.get("resolution_due_at")),
                resolved_at=_none_if_blank(item.get("resolved_at")),
                closed_at=_none_if_blank(item.get("closed_at")),
                data=data,
            )
        if normalized_name == "auth_sessions":
            token_hash = _clean(item.get("token_hash"))
            if not token_hash:
                return None
            return AuthSessionModel(
                id=item_id,
                token_hash=token_hash,
                role=_clean(item.get("role")) or "user",
                key_id=_none_if_blank(item.get("key_id")),
                user_id=_none_if_blank(item.get("user_id")),
                email=_none_if_blank(item.get("email")),
                created_at=_clean(item.get("created_at")),
                last_used_at=_none_if_blank(item.get("last_used_at")),
                expires_at=_none_if_blank(item.get("expires_at")),
                revoked_at=_none_if_blank(item.get("revoked_at")),
                data=data,
            )
        if normalized_name == "auth_action_tokens":
            token_hash = _clean(item.get("token_hash"))
            user_id = _clean(item.get("user_id"))
            if not token_hash or not user_id:
                return None
            return AuthActionTokenModel(
                id=item_id,
                type=_clean(item.get("type")) or "email_verify",
                token_hash=token_hash,
                user_id=user_id,
                email=_none_if_blank(item.get("email")),
                created_at=_clean(item.get("created_at")),
                expires_at=_none_if_blank(item.get("expires_at")),
                used_at=_none_if_blank(item.get("used_at")),
                data=data,
            )
        return None

    def _load_quota_ledger_rows(self) -> list[dict[str, Any]]:
        session = self.Session()
        try:
            items = []
            for row in session.query(QuotaLedgerModel).order_by(QuotaLedgerModel.created_at.asc()).all():
                item_data = _json_load(row.data)
                item_data.setdefault("id", row.id)
                item_data.setdefault("user_id", row.user_id)
                item_data.setdefault("type", row.type)
                item_data.setdefault("amount", row.amount)
                item_data.setdefault("balance_before", row.balance_before)
                item_data.setdefault("balance_after", row.balance_after)
                item_data.setdefault("reason", row.reason)
                item_data.setdefault("ref_type", row.ref_type)
                item_data.setdefault("ref_id", row.ref_id)
                item_data.setdefault("actor_type", row.actor_type)
                item_data.setdefault("actor_id", row.actor_id)
                item_data.setdefault("created_at", row.created_at)
                items.append(item_data)
            return items
        finally:
            session.close()

    def _save_quota_ledger_rows(self, items: list[dict[str, Any]]) -> None:
        session = self.Session()
        try:
            for item in items:
                if not isinstance(item, dict):
                    continue
                item_id = _collection_item_id(item)
                user_id = _clean(item.get("user_id"))
                if not item_id or not user_id:
                    continue
                amount = _safe_int(item.get("amount"))
                balance_before = _safe_non_negative_int(item.get("balance_before"))
                balance_after = _safe_non_negative_int(item.get("balance_after"))
                session.merge(
                    QuotaLedgerModel(
                        id=item_id,
                        user_id=user_id,
                        type=_clean(item.get("type")) or "adjust",
                        amount=amount,
                        balance_before=balance_before,
                        balance_after=balance_after,
                        reason=_none_if_blank(item.get("reason")),
                        ref_type=_none_if_blank(item.get("ref_type")),
                        ref_id=_none_if_blank(item.get("ref_id")),
                        actor_type=_none_if_blank(item.get("actor_type")),
                        actor_id=_none_if_blank(item.get("actor_id")),
                        created_at=_clean(item.get("created_at")),
                        data=_json_data(item),
                    )
                )
            session.commit()
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()

    def _load_rows(self, model: type[AccountModel] | type[AuthKeyModel]) -> list[dict[str, Any]]:
        session = self.Session()
        try:
            items = []
            for row in session.query(model).all():
                item_data = _json_load(row.data)
                if item_data:
                    items.append(item_data)
            return items
        finally:
            session.close()

    def _save_rows(
        self,
        model: type[AccountModel] | type[AuthKeyModel],
        items: list[dict[str, Any]],
        source_key: str,
        target_key: str | None = None,
    ) -> None:
        session = self.Session()
        try:
            session.query(model).delete()
            for item in items:
                if not isinstance(item, dict):
                    continue
                key_value = _clean(item.get(source_key))
                if not key_value:
                    continue
                session.add(model(**{target_key or source_key: key_value}, data=_json_data(item)))
            session.commit()
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()

    def health_check(self) -> dict[str, Any]:
        try:
            session = self.Session()
            try:
                session.execute(text("SELECT 1"))
                dedicated_counts = {
                    name: session.query(model).count()
                    for name, model in DEDICATED_COLLECTION_MODELS.items()
                }
                schema_migrations = get_applied_migrations(self.engine)
                return {
                    "status": "healthy",
                    "backend": "database",
                    "database_url": self._mask_password(self.database_url),
                    "account_count": session.query(AccountModel).count(),
                    "auth_key_count": session.query(AuthKeyModel).count(),
                    "collection_item_count": session.query(CollectionItemModel).count(),
                    "quota_ledger_count": session.query(QuotaLedgerModel).count(),
                    "dedicated_collection_counts": dedicated_counts,
                    "schema_migration_count": len(schema_migrations),
                    "schema_migrations": schema_migrations,
                }
            finally:
                session.close()
        except Exception as e:
            return {"status": "unhealthy", "backend": "database", "error": str(e)}

    def get_backend_info(self) -> dict[str, Any]:
        db_type = "unknown"
        if "sqlite" in self.database_url:
            db_type = "sqlite"
        elif "postgresql" in self.database_url or "postgres" in self.database_url:
            db_type = "postgresql"
        elif "mysql" in self.database_url:
            db_type = "mysql"

        return {
            "type": "database",
            "db_type": db_type,
            "description": f"database storage ({db_type})",
            "database_url": self._mask_password(self.database_url),
            "dedicated_collections": sorted(DEDICATED_COLLECTION_MODELS.keys()),
        }

    @staticmethod
    def _mask_password(url: str) -> str:
        if "://" not in url:
            return url
        try:
            protocol, rest = url.split("://", 1)
            if "@" in rest:
                credentials, host = rest.split("@", 1)
                if ":" in credentials:
                    username, _ = credentials.split(":", 1)
                    return f"{protocol}://{username}:****@{host}"
            return url
        except Exception:
            return url
