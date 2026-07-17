from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Literal

from services.config import config
from services.storage.base import StorageBackend

AuthRole = Literal["admin", "user"]

DEFAULT_USER_PERMISSIONS = [
    "image.generate",
    "image.edit",
    "chat.completions",
    "responses.create",
    "messages.create",
]
DEFAULT_ADMIN_PERMISSIONS = ["*"]
PASSWORD_ITERATIONS = 260_000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        candidate = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if candidate.tzinfo is None:
        candidate = candidate.replace(tzinfo=timezone.utc)
    return candidate.astimezone(timezone.utc)


def _hash_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parse_non_negative_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _normalize_email(value: object) -> str:
    return str(value or "").strip().lower()


def _hash_password(password: str) -> str:
    salt = secrets.token_urlsafe(18)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt}${digest.hex()}"


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = str(encoded or "").split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iterations))
        return hmac.compare_digest(digest.hex(), expected)
    except Exception:
        return False


WEAK_PASSWORDS = {
    "password",
    "password123",
    "12345678",
    "123456789",
    "qwerty123",
    "11111111",
    "admin123",
    "chatgpt2api",
}


def _validate_password(email: str, password: str) -> None:
    candidate = str(password or "")
    normalized = candidate.strip()
    if candidate != normalized:
        raise ValueError("password cannot start or end with whitespace")
    if len(normalized) < 8:
        raise ValueError("password must be at least 8 characters")
    if not normalized:
        raise ValueError("password cannot be blank")
    lowered = normalized.lower()
    email_prefix = _normalize_email(email).split("@", 1)[0]
    if lowered in WEAK_PASSWORDS or (email_prefix and lowered == email_prefix):
        raise ValueError("password is too weak")
    if len(set(lowered)) <= 2:
        raise ValueError("password is too weak")


def _normalize_permissions(value: object, role: AuthRole) -> list[str]:
    if role == "admin":
        return DEFAULT_ADMIN_PERMISSIONS.copy()
    if not isinstance(value, list):
        return DEFAULT_USER_PERMISSIONS.copy()
    permissions = []
    for item in value:
        permission = str(item or "").strip()
        if permission and permission not in permissions:
            permissions.append(permission)
    return permissions or DEFAULT_USER_PERMISSIONS.copy()


def _normalize_metadata(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items() if str(key).strip()}


def _remaining_quota(item: dict[str, object]) -> int | None:
    limit = item.get("quota_limit")
    if limit is None:
        return None
    used = _parse_non_negative_int(item.get("quota_used")) or 0
    return max(int(limit) - used, 0)


def _public_user(item: dict[str, object]) -> dict[str, object]:
    return {
        "id": item.get("id"),
        "email": item.get("email"),
        "name": item.get("name"),
        "role": item.get("role") or "user",
        "enabled": bool(item.get("enabled", True)),
        "quota_balance": _parse_non_negative_int(item.get("quota_balance")) or 0,
        "package_id": item.get("package_id"),
        "package_name": item.get("package_name"),
        "package_expires_at": item.get("package_expires_at"),
        "email_verified_at": item.get("email_verified_at"),
        "email_verified": bool(item.get("email_verified_at")),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "last_login_at": item.get("last_login_at"),
        "last_used_at": item.get("last_used_at"),
    }


class AuthService:
    def __init__(self, storage: StorageBackend):
        self.storage = storage
        self._lock = Lock()
        self._items = self._load()
        self._users = self._load_users()
        self._quota_ledger = self._load_quota_ledger()
        self._sessions = self._load_sessions()
        self._auth_action_tokens = self._load_auth_action_tokens()
        self._last_used_flush_at: dict[str, datetime] = {}

    @staticmethod
    def _clean(value: object) -> str:
        return str(value or "").strip()

    def _normalize_item(self, raw: object) -> dict[str, object] | None:
        if not isinstance(raw, dict):
            return None
        role = self._clean(raw.get("role")).lower()
        if role not in {"admin", "user"}:
            return None
        key_hash = self._clean(raw.get("key_hash"))
        if not key_hash:
            return None
        item_id = self._clean(raw.get("id")) or uuid.uuid4().hex[:12]
        name = self._clean(raw.get("name")) or ("管理员密钥" if role == "admin" else "普通用户")
        created_at = self._clean(raw.get("created_at")) or _now_iso()
        last_used_at = self._clean(raw.get("last_used_at")) or None
        quota_limit = _parse_non_negative_int(raw.get("quota_limit"))
        quota_used = _parse_non_negative_int(raw.get("quota_used"))
        if quota_used is None:
            quota_used = _parse_non_negative_int(raw.get("used_count")) or 0
        rate_limit_per_minute = _parse_non_negative_int(raw.get("rate_limit_per_minute"))
        expires_at = self._clean(raw.get("expires_at")) or None
        normalized_role = role  # type: ignore[assignment]
        return {
            "id": item_id,
            "name": name,
            "role": normalized_role,
            "key_hash": key_hash,
            "enabled": bool(raw.get("enabled", True)),
            "created_at": created_at,
            "last_used_at": last_used_at,
            "permissions": _normalize_permissions(raw.get("permissions"), normalized_role),
            "quota_limit": quota_limit,
            "quota_used": quota_used,
            "quota_remaining": None if quota_limit is None else max(quota_limit - quota_used, 0),
            "rate_limit_per_minute": rate_limit_per_minute,
            "expires_at": expires_at,
            "metadata": _normalize_metadata(raw.get("metadata")),
            "user_id": self._clean(raw.get("user_id")) or None,
            "kind": self._clean(raw.get("kind")) or "manual",
        }

    def _normalize_user(self, raw: object) -> dict[str, object] | None:
        if not isinstance(raw, dict):
            return None
        email = _normalize_email(raw.get("email"))
        password_hash = self._clean(raw.get("password_hash"))
        if not email or not password_hash:
            return None
        now = _now_iso()
        return {
            "id": self._clean(raw.get("id")) or f"usr_{uuid.uuid4().hex[:12]}",
            "email": email,
            "password_hash": password_hash,
            "name": self._clean(raw.get("name")) or email.split("@", 1)[0],
            "role": "user",
            "enabled": bool(raw.get("enabled", True)),
            "quota_balance": _parse_non_negative_int(raw.get("quota_balance")) or 0,
            "package_id": self._clean(raw.get("package_id")) or None,
            "package_name": self._clean(raw.get("package_name")) or None,
            "package_expires_at": self._clean(raw.get("package_expires_at")) or None,
            "email_verified_at": self._clean(raw.get("email_verified_at")) or None,
            "created_at": self._clean(raw.get("created_at")) or now,
            "updated_at": self._clean(raw.get("updated_at")) or now,
            "last_login_at": self._clean(raw.get("last_login_at")) or None,
            "last_used_at": self._clean(raw.get("last_used_at")) or None,
        }

    def _normalize_ledger_item(self, raw: object) -> dict[str, object] | None:
        if not isinstance(raw, dict):
            return None
        user_id = self._clean(raw.get("user_id"))
        if not user_id:
            return None
        item_id = self._clean(raw.get("id")) or f"ql_{uuid.uuid4().hex[:12]}"
        event_type = self._clean(raw.get("type")) or "adjust"
        try:
            amount = int(raw.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0
        return {
            "id": item_id,
            "user_id": user_id,
            "type": event_type,
            "amount": amount,
            "balance_before": _parse_non_negative_int(raw.get("balance_before")) or 0,
            "balance_after": _parse_non_negative_int(raw.get("balance_after")) or 0,
            "reason": self._clean(raw.get("reason")),
            "ref_type": self._clean(raw.get("ref_type")) or None,
            "ref_id": self._clean(raw.get("ref_id")) or None,
            "actor_type": self._clean(raw.get("actor_type")) or None,
            "actor_id": self._clean(raw.get("actor_id")) or None,
            "created_at": self._clean(raw.get("created_at")) or _now_iso(),
            "metadata": _normalize_metadata(raw.get("metadata")),
        }

    def _normalize_session(self, raw: object) -> dict[str, object] | None:
        if not isinstance(raw, dict):
            return None
        token_hash = self._clean(raw.get("token_hash"))
        if not token_hash:
            return None
        session_id = self._clean(raw.get("id")) or f"sess_{uuid.uuid4().hex[:12]}"
        created_at = self._clean(raw.get("created_at")) or _now_iso()
        role = self._clean(raw.get("role")).lower()
        if role not in {"admin", "user"}:
            role = "user"
        return {
            "id": session_id,
            "token_hash": token_hash,
            "role": role,
            "key_id": self._clean(raw.get("key_id")) or None,
            "user_id": self._clean(raw.get("user_id")) or None,
            "name": self._clean(raw.get("name")) or None,
            "email": _normalize_email(raw.get("email")) or None,
            "created_at": created_at,
            "last_used_at": self._clean(raw.get("last_used_at")) or None,
            "expires_at": self._clean(raw.get("expires_at")) or None,
            "revoked_at": self._clean(raw.get("revoked_at")) or None,
            "metadata": _normalize_metadata(raw.get("metadata")),
        }

    def _normalize_auth_action_token(self, raw: object) -> dict[str, object] | None:
        if not isinstance(raw, dict):
            return None
        token_hash = self._clean(raw.get("token_hash"))
        token_type = self._clean(raw.get("type"))
        user_id = self._clean(raw.get("user_id"))
        if not token_hash or token_type not in {"email_verify", "password_reset"} or not user_id:
            return None
        return {
            "id": self._clean(raw.get("id")) or f"tok_{uuid.uuid4().hex[:12]}",
            "type": token_type,
            "token_hash": token_hash,
            "user_id": user_id,
            "email": _normalize_email(raw.get("email")) or None,
            "created_at": self._clean(raw.get("created_at")) or _now_iso(),
            "expires_at": self._clean(raw.get("expires_at")) or None,
            "used_at": self._clean(raw.get("used_at")) or None,
            "metadata": _normalize_metadata(raw.get("metadata")),
        }

    def _load(self) -> list[dict[str, object]]:
        try:
            items = self.storage.load_auth_keys()
        except Exception:
            return []
        if not isinstance(items, list):
            return []
        return [normalized for item in items if (normalized := self._normalize_item(item)) is not None]

    def _load_users(self) -> list[dict[str, object]]:
        try:
            items = self.storage.load_collection("users")
        except Exception:
            return []
        if not isinstance(items, list):
            return []
        return [normalized for item in items if (normalized := self._normalize_user(item)) is not None]

    def _load_quota_ledger(self) -> list[dict[str, object]]:
        try:
            items = self.storage.load_collection("quota_ledger")
        except Exception:
            return []
        if not isinstance(items, list):
            return []
        return [normalized for item in items if (normalized := self._normalize_ledger_item(item)) is not None]

    def _load_sessions(self) -> list[dict[str, object]]:
        try:
            items = self.storage.load_collection("auth_sessions")
        except Exception:
            return []
        if not isinstance(items, list):
            return []
        return [normalized for item in items if (normalized := self._normalize_session(item)) is not None]

    def _load_auth_action_tokens(self) -> list[dict[str, object]]:
        try:
            items = self.storage.load_collection("auth_action_tokens")
        except Exception:
            return []
        if not isinstance(items, list):
            return []
        return [normalized for item in items if (normalized := self._normalize_auth_action_token(item)) is not None]

    def _save(self) -> None:
        self.storage.save_auth_keys(self._items)

    def _save_users(self) -> None:
        self.storage.save_collection("users", self._users)

    def _save_quota_ledger(self) -> None:
        self.storage.save_collection("quota_ledger", self._quota_ledger)

    def _save_quota_ledger_item(self, item: dict[str, object]) -> None:
        self.storage.append_collection_item("quota_ledger", item)

    def _save_session(self, item: dict[str, object]) -> None:
        self.storage.append_collection_item("auth_sessions", item)

    def _save_auth_action_token(self, item: dict[str, object]) -> None:
        self.storage.append_collection_item("auth_action_tokens", item)

    @staticmethod
    def _public_ledger_item(item: dict[str, object]) -> dict[str, object]:
        return {
            "id": item.get("id"),
            "user_id": item.get("user_id"),
            "type": item.get("type"),
            "amount": item.get("amount"),
            "balance_before": item.get("balance_before"),
            "balance_after": item.get("balance_after"),
            "reason": item.get("reason"),
            "ref_type": item.get("ref_type"),
            "ref_id": item.get("ref_id"),
            "actor_type": item.get("actor_type"),
            "actor_id": item.get("actor_id"),
            "created_at": item.get("created_at"),
            "metadata": dict(item.get("metadata") or {}),
        }

    def _record_quota_ledger(
        self,
        *,
        user_id: str,
        event_type: str,
        amount: int,
        balance_before: int,
        balance_after: int,
        reason: str = "",
        ref_type: str | None = None,
        ref_id: str | None = None,
        actor_type: str | None = None,
        actor_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        item = {
            "id": f"ql_{uuid.uuid4().hex[:12]}",
            "user_id": user_id,
            "type": self._clean(event_type) or "adjust",
            "amount": int(amount or 0),
            "balance_before": max(0, int(balance_before or 0)),
            "balance_after": max(0, int(balance_after or 0)),
            "reason": self._clean(reason),
            "ref_type": self._clean(ref_type) or None,
            "ref_id": self._clean(ref_id) or None,
            "actor_type": self._clean(actor_type) or None,
            "actor_id": self._clean(actor_id) or None,
            "created_at": _now_iso(),
            "metadata": _normalize_metadata(metadata),
        }
        self._quota_ledger.append(item)
        return item

    @staticmethod
    def _public_item(item: dict[str, object]) -> dict[str, object]:
        public = {
            "id": item.get("id"),
            "name": item.get("name"),
            "role": item.get("role"),
            "enabled": bool(item.get("enabled", True)),
            "created_at": item.get("created_at"),
            "last_used_at": item.get("last_used_at"),
            "permissions": list(item.get("permissions") or []),
            "quota_limit": item.get("quota_limit"),
            "quota_used": _parse_non_negative_int(item.get("quota_used")) or 0,
            "quota_remaining": _remaining_quota(item),
            "rate_limit_per_minute": item.get("rate_limit_per_minute"),
            "expires_at": item.get("expires_at"),
            "metadata": dict(item.get("metadata") or {}),
            "user_id": item.get("user_id"),
            "kind": item.get("kind") or "manual",
        }
        return public

    def _find_user(self, user_id: str) -> tuple[int, dict[str, object]] | tuple[None, None]:
        for index, user in enumerate(self._users):
            if user.get("id") == user_id:
                return index, user
        return None, None

    def _find_user_by_email(self, email: str) -> tuple[int, dict[str, object]] | tuple[None, None]:
        normalized_email = _normalize_email(email)
        for index, user in enumerate(self._users):
            if user.get("email") == normalized_email:
                return index, user
        return None, None

    def _find_key(self, key_id: str) -> tuple[int, dict[str, object]] | tuple[None, None]:
        normalized_id = self._clean(key_id)
        for index, item in enumerate(self._items):
            if item.get("id") == normalized_id:
                return index, item
        return None, None

    def _key_identity_locked(
        self,
        item: dict[str, object],
        *,
        now: datetime | None = None,
        update_last_used: bool = True,
        reject_expired: bool = False,
    ) -> dict[str, object] | None:
        if not bool(item.get("enabled", True)):
            return None
        expires_at = _parse_datetime(item.get("expires_at"))
        current = now or _now()
        if reject_expired and expires_at is not None and expires_at <= current:
            return None
        item_index, _ = self._find_key(str(item.get("id") or ""))
        user_id = self._clean(item.get("user_id"))
        user = None
        user_index = None
        if user_id:
            user_index, user = self._find_user(user_id)
            if user is None or not bool(user.get("enabled", True)):
                return None
        next_item = dict(item)
        if update_last_used and item_index is not None:
            next_item["last_used_at"] = current.isoformat()
            self._items[item_index] = next_item
        if update_last_used and user is not None and user_index is not None:
            next_user = dict(user)
            next_user["last_used_at"] = current.isoformat()
            self._users[user_index] = next_user
            user = next_user
        item_id = self._clean(next_item.get("id"))
        last_flush_at = self._last_used_flush_at.get(item_id)
        if update_last_used and (last_flush_at is None or (current - last_flush_at).total_seconds() >= 60):
            try:
                self._save()
                if user is not None:
                    self._save_users()
                self._last_used_flush_at[item_id] = current
            except Exception:
                pass
        identity = self._public_item(next_item)
        identity["key_id"] = identity.get("id")
        if user is not None:
            public_user = _public_user(user)
            identity.update({
                "id": next_item.get("id"),
                "user_id": public_user.get("id"),
                "email": public_user.get("email"),
                "name": public_user.get("name") or identity.get("name"),
                "quota_balance": public_user.get("quota_balance"),
                "package_id": public_user.get("package_id"),
                "package_name": public_user.get("package_name"),
                "package_expires_at": public_user.get("package_expires_at"),
                "email_verified_at": public_user.get("email_verified_at"),
                "email_verified": public_user.get("email_verified"),
            })
        return identity

    def _new_login_key(self, user: dict[str, object]) -> tuple[dict[str, object], str]:
        raw_key = f"sk-{secrets.token_urlsafe(24)}"
        user_id = str(user.get("id") or "")
        key = None
        for item in self._items:
            if item.get("user_id") == user_id and item.get("kind") == "login":
                key = item
                break
        now = _now_iso()
        if key is None:
            key = {
                "id": f"key_{uuid.uuid4().hex[:12]}",
                "created_at": now,
                "last_used_at": None,
            }
            self._items.append(key)
        key.update({
            "name": str(user.get("email") or user.get("name") or "注册用户"),
            "role": "user",
            "key_hash": _hash_key(raw_key),
            "enabled": True,
            "permissions": DEFAULT_USER_PERMISSIONS.copy(),
            "quota_limit": None,
            "quota_used": 0,
            "quota_remaining": None,
            "rate_limit_per_minute": None,
            "expires_at": None,
            "metadata": {},
            "user_id": user_id,
            "kind": "login",
        })
        return key, raw_key

    def list_keys(self, role: AuthRole | None = None) -> list[dict[str, object]]:
        with self._lock:
            items = [item for item in self._items if role is None or item.get("role") == role]
            return [self._public_item(item) for item in items]

    def list_users(self) -> list[dict[str, object]]:
        with self._lock:
            return [_public_user(user) for user in self._users]

    def list_quota_ledger(self, user_id: str | None = None, limit: int = 200) -> list[dict[str, object]]:
        normalized_user_id = self._clean(user_id) if user_id else ""
        safe_limit = min(max(1, int(limit or 200)), 1000)
        with self._lock:
            items = list(self._quota_ledger)
            if normalized_user_id:
                items = [item for item in items if item.get("user_id") == normalized_user_id]
            items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
            return [self._public_ledger_item(item) for item in items[:safe_limit]]

    def create_session(self, identity: dict[str, object], *, ttl_seconds: int = 60 * 60 * 24 * 30) -> tuple[dict[str, object], str]:
        role = self._clean(identity.get("role")).lower()
        if role not in {"admin", "user"}:
            raise ValueError("identity role is invalid")
        raw_token = f"sess-{secrets.token_urlsafe(32)}"
        now = _now()
        expires_at = now + timedelta(seconds=max(60, int(ttl_seconds or 0)))
        item = {
            "id": f"sess_{uuid.uuid4().hex[:12]}",
            "token_hash": _hash_key(raw_token),
            "role": role,
            "key_id": self._clean(identity.get("key_id")) or None,
            "user_id": self._clean(identity.get("user_id")) or None,
            "name": self._clean(identity.get("name")) or None,
            "email": _normalize_email(identity.get("email")) or None,
            "created_at": now.isoformat(),
            "last_used_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "revoked_at": None,
            "metadata": {"source": "http-cookie"},
        }
        with self._lock:
            self._sessions.append(item)
            self._save_session(item)
            return {key: value for key, value in item.items() if key != "token_hash"}, raw_token

    def revoke_session(self, raw_token: str) -> bool:
        candidate_hash = _hash_key(self._clean(raw_token))
        with self._lock:
            for index, item in enumerate(self._sessions):
                if item.get("token_hash") != candidate_hash or item.get("revoked_at"):
                    continue
                next_item = dict(item)
                next_item["revoked_at"] = _now_iso()
                self._sessions[index] = next_item
                self._save_session(next_item)
                return True
        return False

    def revoke_user_sessions(self, user_id: str) -> int:
        normalized_user_id = self._clean(user_id)
        if not normalized_user_id:
            return 0
        revoked = 0
        now = _now_iso()
        with self._lock:
            for index, item in enumerate(list(self._sessions)):
                if item.get("user_id") != normalized_user_id or item.get("revoked_at"):
                    continue
                next_item = dict(item)
                next_item["revoked_at"] = now
                self._sessions[index] = next_item
                self._save_session(next_item)
                revoked += 1
        return revoked

    def _session_identity_locked(self, raw_token: str) -> dict[str, object] | None:
        candidate_hash = _hash_key(self._clean(raw_token))
        now = _now()
        for index, item in enumerate(self._sessions):
            if item.get("token_hash") != candidate_hash:
                continue
            if item.get("revoked_at"):
                return None
            expires_at = _parse_datetime(item.get("expires_at"))
            if expires_at is not None and expires_at <= now:
                return None
            next_session = dict(item)
            next_session["last_used_at"] = now.isoformat()
            self._sessions[index] = next_session
            try:
                self._save_session(next_session)
            except Exception:
                pass
            key_id = self._clean(item.get("key_id"))
            if key_id:
                _, key = self._find_key(key_id)
                if key is None:
                    return None
                identity = self._key_identity_locked(key, now=now, update_last_used=True, reject_expired=True)
                if identity is None:
                    return None
            elif item.get("role") == "admin":
                identity = {
                    "id": item.get("id"),
                    "key_id": item.get("id"),
                    "role": "admin",
                    "name": item.get("name") or "admin",
                    "permissions": ["*"],
                    "quota_limit": None,
                    "quota_used": 0,
                    "quota_remaining": None,
                    "rate_limit_per_minute": None,
                }
            else:
                return None
            identity["session_id"] = item.get("id")
            identity["session_expires_at"] = item.get("expires_at")
            identity["expires_at"] = item.get("expires_at")
            return identity
        return None

    @staticmethod
    def _public_auth_action_token(item: dict[str, object]) -> dict[str, object]:
        return {
            "id": item.get("id"),
            "type": item.get("type"),
            "user_id": item.get("user_id"),
            "email": item.get("email"),
            "created_at": item.get("created_at"),
            "expires_at": item.get("expires_at"),
            "used_at": item.get("used_at"),
        }

    def create_email_verification_token(self, email_or_user_id: str, *, ttl_seconds: int = 60 * 60 * 24) -> tuple[dict[str, object], str]:
        subject = self._clean(email_or_user_id)
        with self._lock:
            _, user = self._find_user(subject)
            if user is None:
                _, user = self._find_user_by_email(subject)
            if user is None:
                raise ValueError("user not found")
            raw_token = f"ev-{secrets.token_urlsafe(32)}"
            now = _now()
            item = {
                "id": f"tok_{uuid.uuid4().hex[:12]}",
                "type": "email_verify",
                "token_hash": _hash_key(raw_token),
                "user_id": user.get("id"),
                "email": user.get("email"),
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(seconds=max(60, int(ttl_seconds or 0)))).isoformat(),
                "used_at": None,
                "metadata": {},
            }
            self._auth_action_tokens.append(item)
            self._save_auth_action_token(item)
            return self._public_auth_action_token(item), raw_token

    def verify_email_token(self, raw_token: str) -> dict[str, object]:
        return self._consume_action_token(raw_token, "email_verify", new_password=None)

    def create_password_reset_token(self, email: str, *, ttl_seconds: int = 60 * 30) -> tuple[dict[str, object] | None, str | None]:
        normalized_email = _normalize_email(email)
        with self._lock:
            _, user = self._find_user_by_email(normalized_email)
            if user is None:
                return None, None
            raw_token = f"pr-{secrets.token_urlsafe(32)}"
            now = _now()
            item = {
                "id": f"tok_{uuid.uuid4().hex[:12]}",
                "type": "password_reset",
                "token_hash": _hash_key(raw_token),
                "user_id": user.get("id"),
                "email": user.get("email"),
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(seconds=max(60, int(ttl_seconds or 0)))).isoformat(),
                "used_at": None,
                "metadata": {},
            }
            self._auth_action_tokens.append(item)
            self._save_auth_action_token(item)
            return self._public_auth_action_token(item), raw_token

    def reset_password_with_token(self, raw_token: str, new_password: str) -> dict[str, object]:
        return self._consume_action_token(raw_token, "password_reset", new_password=new_password)

    def _consume_action_token(self, raw_token: str, token_type: str, *, new_password: str | None) -> dict[str, object]:
        token_hash = _hash_key(self._clean(raw_token))
        now = _now()
        with self._lock:
            for index, item in enumerate(self._auth_action_tokens):
                if item.get("token_hash") != token_hash or item.get("type") != token_type:
                    continue
                if item.get("used_at"):
                    raise ValueError("token already used")
                expires_at = _parse_datetime(item.get("expires_at"))
                if expires_at is not None and expires_at <= now:
                    raise ValueError("token expired")
                user_index, user = self._find_user(str(item.get("user_id") or ""))
                if user is None or user_index is None or not bool(user.get("enabled", True)):
                    raise ValueError("user not found")
                next_user = dict(user)
                if token_type == "email_verify":
                    next_user["email_verified_at"] = now.isoformat()
                elif token_type == "password_reset":
                    if not new_password:
                        raise ValueError("password is required")
                    _validate_password(str(next_user.get("email") or ""), new_password)
                    next_user["password_hash"] = _hash_password(new_password)
                    self._revoke_user_sessions_locked(str(next_user.get("id") or ""), now=now.isoformat())
                    for key_index, key_item in enumerate(list(self._items)):
                        if key_item.get("user_id") == next_user.get("id") and key_item.get("kind") == "login":
                            next_key = dict(key_item)
                            next_key["key_hash"] = _hash_key(f"revoked-{secrets.token_urlsafe(32)}")
                            next_key["last_used_at"] = now.isoformat()
                            self._items[key_index] = next_key
                next_user["updated_at"] = now.isoformat()
                self._users[user_index] = next_user
                next_item = dict(item)
                next_item["used_at"] = now.isoformat()
                self._auth_action_tokens[index] = next_item
                self._save_users()
                if token_type == "password_reset":
                    self._save()
                self._save_auth_action_token(next_item)
                return _public_user(next_user)
        raise ValueError("token is invalid")

    def _revoke_user_sessions_locked(self, user_id: str, *, now: str) -> int:
        revoked = 0
        for index, item in enumerate(list(self._sessions)):
            if item.get("user_id") != user_id or item.get("revoked_at"):
                continue
            next_item = dict(item)
            next_item["revoked_at"] = now
            self._sessions[index] = next_item
            self._save_session(next_item)
            revoked += 1
        return revoked

    def register_user(self, email: str, password: str, name: str = "") -> tuple[dict[str, object], str, dict[str, object]]:
        normalized_email = _normalize_email(email)
        if "@" not in normalized_email or "." not in normalized_email.rsplit("@", 1)[-1]:
            raise ValueError("email is invalid")
        _validate_password(normalized_email, password)
        with self._lock:
            _, existing = self._find_user_by_email(normalized_email)
            if existing is not None:
                raise ValueError("email already registered")
            now = _now_iso()
            user = {
                "id": f"usr_{uuid.uuid4().hex[:12]}",
                "email": normalized_email,
                "password_hash": _hash_password(password),
                "name": self._clean(name) or normalized_email.split("@", 1)[0],
                "role": "user",
                "enabled": True,
                "quota_balance": 0,
                "package_id": None,
                "package_name": None,
                "package_expires_at": None,
                "email_verified_at": None,
                "created_at": now,
                "updated_at": now,
                "last_login_at": now,
                "last_used_at": None,
            }
            self._users.append(user)
            key, raw_key = self._new_login_key(user)
            self._save_users()
            self._save()
            return _public_user(user), raw_key, self._public_item(key)

    def login_user(self, email: str, password: str) -> tuple[dict[str, object], str, dict[str, object]]:
        normalized_email = _normalize_email(email)
        with self._lock:
            index, user = self._find_user_by_email(normalized_email)
            if user is None or not bool(user.get("enabled", True)) or not _verify_password(password, str(user.get("password_hash") or "")):
                raise ValueError("email or password is invalid")
            next_user = dict(user)
            now = _now_iso()
            next_user["last_login_at"] = now
            next_user["updated_at"] = now
            self._users[index] = next_user  # type: ignore[index]
            key, raw_key = self._new_login_key(next_user)
            self._save_users()
            self._save()
            return _public_user(next_user), raw_key, self._public_item(key)

    def update_user(self, user_id: str, updates: dict[str, object]) -> dict[str, object] | None:
        with self._lock:
            index, user = self._find_user(self._clean(user_id))
            if user is None:
                return None
            next_user = dict(user)
            if "name" in updates and updates.get("name") is not None:
                next_user["name"] = self._clean(updates.get("name")) or next_user.get("name")
            if "enabled" in updates and updates.get("enabled") is not None:
                next_user["enabled"] = bool(updates.get("enabled"))
            quota_balance_changed = False
            balance_before = _parse_non_negative_int(next_user.get("quota_balance")) or 0
            if "quota_balance" in updates and updates.get("quota_balance") is not None:
                next_user["quota_balance"] = _parse_non_negative_int(updates.get("quota_balance")) or 0
                quota_balance_changed = True
            for key in ("package_id", "package_name", "package_expires_at", "email_verified_at"):
                if key in updates:
                    next_user[key] = self._clean(updates.get(key)) or None
            if "password" in updates and updates.get("password"):
                _validate_password(str(next_user.get("email") or ""), str(updates.get("password")))
                next_user["password_hash"] = _hash_password(str(updates.get("password")))
            next_user["updated_at"] = _now_iso()
            self._users[index] = next_user  # type: ignore[index]
            if quota_balance_changed:
                balance_after = _parse_non_negative_int(next_user.get("quota_balance")) or 0
                if balance_after != balance_before:
                    ledger_item = self._record_quota_ledger(
                        user_id=str(next_user.get("id") or ""),
                        event_type="set",
                        amount=balance_after - balance_before,
                        balance_before=balance_before,
                        balance_after=balance_after,
                        reason="admin-set",
                        ref_type="admin_user_update",
                    )
                    self._save_quota_ledger_item(ledger_item)
            self._save_users()
            return _public_user(next_user)

    def adjust_user_quota(
        self,
        user_id: str,
        delta: int,
        reason: str = "",
        *,
        ref_type: str | None = None,
        ref_id: str | None = None,
        actor_type: str | None = None,
        actor_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        with self._lock:
            index, user = self._find_user(self._clean(user_id))
            if user is None:
                return None
            next_user = dict(user)
            current = _parse_non_negative_int(next_user.get("quota_balance")) or 0
            balance_after = max(0, current + int(delta or 0))
            actual_delta = balance_after - current
            next_user["quota_balance"] = balance_after
            next_user["updated_at"] = _now_iso()
            self._users[index] = next_user  # type: ignore[index]
            if actual_delta:
                event_type = "refund" if self._clean(reason).lower() == "refund" else ("grant" if actual_delta > 0 else "adjust")
                ledger_item = self._record_quota_ledger(
                    user_id=str(next_user.get("id") or ""),
                    event_type=event_type,
                    amount=actual_delta,
                    balance_before=current,
                    balance_after=balance_after,
                    reason=reason,
                    ref_type=ref_type,
                    ref_id=ref_id,
                    actor_type=actor_type,
                    actor_id=actor_id,
                    metadata=metadata,
                )
                self._save_quota_ledger_item(ledger_item)
            self._save_users()
            return _public_user(next_user)

    def try_consume_user_quota(
        self,
        user_id: str,
        units: int,
        *,
        reason: str = "image-generation",
        ref_type: str | None = None,
        ref_id: str | None = None,
        actor_type: str | None = None,
        actor_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> bool:
        count = max(1, int(units or 1))
        with self._lock:
            index, user = self._find_user(self._clean(user_id))
            if user is None or not bool(user.get("enabled", True)):
                return False
            current = _parse_non_negative_int(user.get("quota_balance")) or 0
            if current < count:
                return False
            next_user = dict(user)
            balance_after = current - count
            next_user["quota_balance"] = balance_after
            next_user["last_used_at"] = _now_iso()
            next_user["updated_at"] = next_user["last_used_at"]
            self._users[index] = next_user  # type: ignore[index]
            ledger_item = self._record_quota_ledger(
                user_id=str(next_user.get("id") or ""),
                event_type="consume",
                amount=-count,
                balance_before=current,
                balance_after=balance_after,
                reason=reason,
                ref_type=ref_type,
                ref_id=ref_id,
                actor_type=actor_type,
                actor_id=actor_id,
                metadata=metadata,
            )
            self._save_quota_ledger_item(ledger_item)
            self._save_users()
            return True

    def refund_user_quota(
        self,
        user_id: str,
        units: int,
        *,
        reason: str = "refund",
        ref_type: str | None = None,
        ref_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        self.adjust_user_quota(user_id, max(0, int(units or 0)), reason, ref_type=ref_type, ref_id=ref_id, metadata=metadata)

    def create_key(
        self,
        *,
        role: AuthRole,
        name: str = "",
        permissions: list[str] | None = None,
        quota_limit: int | None = None,
        rate_limit_per_minute: int | None = None,
        expires_at: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> tuple[dict[str, object], str]:
        normalized_name = self._clean(name) or ("管理员密钥" if role == "admin" else "普通用户")
        raw_key = f"sk-{secrets.token_urlsafe(24)}"
        item = {
            "id": uuid.uuid4().hex[:12],
            "name": normalized_name,
            "role": role,
            "key_hash": _hash_key(raw_key),
            "enabled": True,
            "created_at": _now_iso(),
            "last_used_at": None,
            "permissions": _normalize_permissions(permissions, role),
            "quota_limit": _parse_non_negative_int(quota_limit),
            "quota_used": 0,
            "rate_limit_per_minute": _parse_non_negative_int(rate_limit_per_minute),
            "expires_at": self._clean(expires_at) or None,
            "metadata": _normalize_metadata(metadata),
            "user_id": None,
            "kind": "manual",
        }
        item["quota_remaining"] = _remaining_quota(item)
        with self._lock:
            self._items.append(item)
            self._save()
            return self._public_item(item), raw_key

    def update_key(self, key_id: str, updates: dict[str, object], *, role: AuthRole | None = None) -> dict[str, object] | None:
        normalized_id = self._clean(key_id)
        if not normalized_id:
            return None
        with self._lock:
            for index, item in enumerate(self._items):
                if item.get("id") != normalized_id:
                    continue
                if role is not None and item.get("role") != role:
                    return None
                next_item = dict(item)
                if "name" in updates and updates.get("name") is not None:
                    next_item["name"] = self._clean(updates.get("name")) or next_item.get("name") or "普通用户"
                if "enabled" in updates and updates.get("enabled") is not None:
                    next_item["enabled"] = bool(updates.get("enabled"))
                if "permissions" in updates and updates.get("permissions") is not None:
                    next_item["permissions"] = _normalize_permissions(updates.get("permissions"), next_item.get("role"))  # type: ignore[arg-type]
                if "quota_limit" in updates:
                    next_item["quota_limit"] = _parse_non_negative_int(updates.get("quota_limit"))
                if "quota_used" in updates and updates.get("quota_used") is not None:
                    next_item["quota_used"] = _parse_non_negative_int(updates.get("quota_used")) or 0
                if "rate_limit_per_minute" in updates:
                    next_item["rate_limit_per_minute"] = _parse_non_negative_int(updates.get("rate_limit_per_minute"))
                if "expires_at" in updates:
                    next_item["expires_at"] = self._clean(updates.get("expires_at")) or None
                if "metadata" in updates and updates.get("metadata") is not None:
                    next_item["metadata"] = _normalize_metadata(updates.get("metadata"))
                if "add_quota" in updates and updates.get("add_quota") is not None:
                    add_quota = _parse_non_negative_int(updates.get("add_quota")) or 0
                    current_limit = _parse_non_negative_int(next_item.get("quota_limit")) or 0
                    next_item["quota_limit"] = current_limit + add_quota
                if updates.get("reset_quota_used"):
                    next_item["quota_used"] = 0
                next_item["quota_remaining"] = _remaining_quota(next_item)
                self._items[index] = next_item
                self._save()
                return self._public_item(next_item)
        return None

    def delete_key(self, key_id: str, *, role: AuthRole | None = None) -> bool:
        normalized_id = self._clean(key_id)
        if not normalized_id:
            return False
        with self._lock:
            before = len(self._items)
            self._items = [item for item in self._items if not (item.get("id") == normalized_id and (role is None or item.get("role") == role))]
            if len(self._items) == before:
                return False
            self._save()
            return True

    def authenticate(self, raw_key: str) -> dict[str, object] | None:
        candidate = self._clean(raw_key)
        if not candidate:
            return None
        candidate_hash = _hash_key(candidate)
        with self._lock:
            if candidate.startswith("sess-"):
                session_identity = self._session_identity_locked(candidate)
                if session_identity is not None:
                    return session_identity
            for _, item in enumerate(self._items):
                stored_hash = self._clean(item.get("key_hash"))
                if not stored_hash or not hmac.compare_digest(stored_hash, candidate_hash):
                    continue
                return self._key_identity_locked(item, now=_now(), update_last_used=True)
        return None

    def has_permission(self, identity: dict[str, object], permission: str) -> bool:
        if identity.get("role") == "admin":
            return True
        permissions = {str(item) for item in identity.get("permissions") or []}
        if not permissions:
            permissions = set(DEFAULT_USER_PERMISSIONS)
        return "*" in permissions or permission in permissions

    def is_expired(self, identity: dict[str, object]) -> bool:
        expires_at = self._clean(identity.get("expires_at"))
        if not expires_at:
            return False
        try:
            candidate = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        if candidate.tzinfo is None:
            candidate = candidate.replace(tzinfo=timezone.utc)
        return candidate <= datetime.now(timezone.utc)

    def check_quota(self, identity: dict[str, object], units: int) -> bool:
        if identity.get("role") == "admin":
            return True
        if identity.get("user_id"):
            return (_parse_non_negative_int(identity.get("quota_balance")) or 0) >= max(1, int(units or 1))
        remaining = identity.get("quota_remaining")
        return remaining is None or int(remaining) >= max(1, int(units or 1))

    def consume_quota(self, key_id: str, units: int) -> dict[str, object] | None:
        normalized_id = self._clean(key_id)
        count = max(0, int(units or 0))
        if not normalized_id or count <= 0:
            return None
        with self._lock:
            for index, item in enumerate(self._items):
                if item.get("id") != normalized_id:
                    continue
                if item.get("role") == "admin":
                    return self._public_item(item)
                next_item = dict(item)
                next_item["quota_used"] = (_parse_non_negative_int(next_item.get("quota_used")) or 0) + count
                next_item["quota_remaining"] = _remaining_quota(next_item)
                self._items[index] = next_item
                self._save()
                return self._public_item(next_item)
        return None


auth_service = AuthService(config.get_storage_backend())
