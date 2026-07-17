from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from threading import Lock

from services.auth_service import auth_service
from services.config import config
from services.storage.base import StorageBackend

CDK_TYPES = {"quota", "package"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(value: object) -> str:
    return str(value or "").strip()


def _parse_int(value: object, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _parse_currency(value: object) -> str:
    currency = _clean(value).upper() or "CNY"
    return "".join(ch for ch in currency if ch.isalnum())[:8] or "CNY"


def _parse_optional_datetime(value: object) -> datetime | None:
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


def _normalize_code(value: str) -> str:
    return "".join(ch for ch in str(value or "").upper().strip() if not ch.isspace())


def _hash_code(value: str) -> str:
    return hashlib.sha256(_normalize_code(value).encode("utf-8")).hexdigest()


def _generate_code() -> str:
    raw = secrets.token_urlsafe(18).replace("_", "").replace("-", "").upper()[:20]
    return "-".join(raw[index:index + 4] for index in range(0, len(raw), 4))


class RedemptionService:
    def __init__(self, storage: StorageBackend):
        self.storage = storage
        self._lock = Lock()
        self._packages = self._load("packages")
        self._cdks = self._load("cdks")
        self._redemptions = self._load("redemptions")

    def _load(self, name: str) -> list[dict[str, object]]:
        try:
            items = self.storage.load_collection(name)
        except Exception:
            return []
        return [item for item in items if isinstance(item, dict)]

    def _save_packages(self) -> None:
        self.storage.save_collection("packages", self._packages)

    def _save_cdks(self) -> None:
        self.storage.save_collection("cdks", self._cdks)

    def _save_redemptions(self) -> None:
        self.storage.save_collection("redemptions", self._redemptions)

    @staticmethod
    def _public_package(item: dict[str, object]) -> dict[str, object]:
        return {
            "id": item.get("id"),
            "name": item.get("name"),
            "description": item.get("description"),
            "quota": _parse_int(item.get("quota")),
            "price_cents": _parse_int(item.get("price_cents")),
            "currency": _parse_currency(item.get("currency")),
            "valid_days": item.get("valid_days"),
            "enabled": bool(item.get("enabled", True)),
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
        }

    @staticmethod
    def _public_cdk(item: dict[str, object]) -> dict[str, object]:
        return {
            "id": item.get("id"),
            "code_prefix": item.get("code_prefix"),
            "name": item.get("name"),
            "type": item.get("type"),
            "quota": _parse_int(item.get("quota")),
            "package_id": item.get("package_id"),
            "package_name": item.get("package_name"),
            "max_redemptions": _parse_int(item.get("max_redemptions"), 1),
            "redeemed_count": _parse_int(item.get("redeemed_count")),
            "per_user_limit": _parse_int(item.get("per_user_limit"), 1),
            "enabled": bool(item.get("enabled", True)),
            "starts_at": item.get("starts_at"),
            "expires_at": item.get("expires_at"),
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
            "last_redeemed_at": item.get("last_redeemed_at"),
        }

    def list_packages(self) -> list[dict[str, object]]:
        with self._lock:
            return [self._public_package(item) for item in self._packages]

    def get_package(self, package_id: str, *, include_disabled: bool = False) -> dict[str, object] | None:
        normalized_id = _clean(package_id)
        if not normalized_id:
            return None
        with self._lock:
            for item in self._packages:
                if item.get("id") != normalized_id:
                    continue
                if not include_disabled and not bool(item.get("enabled", True)):
                    return None
                return self._public_package(item)
        return None

    def create_package(
        self,
        *,
        name: str,
        description: str = "",
        quota: int = 0,
        valid_days: int | None = None,
        price_cents: int = 0,
        currency: str = "CNY",
    ) -> dict[str, object]:
        now = _now_iso()
        item = {
            "id": f"pkg_{uuid.uuid4().hex[:12]}",
            "name": _clean(name) or "未命名套餐",
            "description": _clean(description),
            "quota": _parse_int(quota),
            "price_cents": _parse_int(price_cents),
            "currency": _parse_currency(currency),
            "valid_days": _parse_int(valid_days) if valid_days is not None else None,
            "enabled": True,
            "created_at": now,
            "updated_at": now,
        }
        with self._lock:
            self._packages.append(item)
            self._save_packages()
            return self._public_package(item)

    def update_package(self, package_id: str, updates: dict[str, object]) -> dict[str, object] | None:
        with self._lock:
            for index, item in enumerate(self._packages):
                if item.get("id") != package_id:
                    continue
                next_item = dict(item)
                for key in ("name", "description"):
                    if key in updates and updates.get(key) is not None:
                        next_item[key] = _clean(updates.get(key))
                if "quota" in updates and updates.get("quota") is not None:
                    next_item["quota"] = _parse_int(updates.get("quota"))
                if "price_cents" in updates and updates.get("price_cents") is not None:
                    next_item["price_cents"] = _parse_int(updates.get("price_cents"))
                if "currency" in updates and updates.get("currency") is not None:
                    next_item["currency"] = _parse_currency(updates.get("currency"))
                if "valid_days" in updates:
                    next_item["valid_days"] = _parse_int(updates.get("valid_days")) if updates.get("valid_days") is not None else None
                if "enabled" in updates and updates.get("enabled") is not None:
                    next_item["enabled"] = bool(updates.get("enabled"))
                next_item["updated_at"] = _now_iso()
                self._packages[index] = next_item
                self._save_packages()
                return self._public_package(next_item)
        return None

    def list_cdks(self) -> list[dict[str, object]]:
        with self._lock:
            return [self._public_cdk(item) for item in self._cdks]

    def create_cdks(
        self,
        *,
        name: str,
        type: str,
        count: int = 1,
        quota: int = 0,
        package_id: str | None = None,
        max_redemptions: int = 1,
        per_user_limit: int = 1,
        expires_at: str | None = None,
    ) -> dict[str, object]:
        cdk_type = _clean(type).lower()
        if cdk_type not in CDK_TYPES:
            raise ValueError("cdk type is invalid")
        count = min(max(1, int(count or 1)), 500)
        package = None
        if cdk_type == "package":
            package = next((item for item in self._packages if item.get("id") == package_id and bool(item.get("enabled", True))), None)
            if package is None:
                raise ValueError("package is invalid")
            quota = _parse_int(package.get("quota"))
        now = _now_iso()
        created: list[dict[str, object]] = []
        raw_codes: list[str] = []
        with self._lock:
            existing_hashes = {str(item.get("code_hash")) for item in self._cdks}
            for _ in range(count):
                code = _generate_code()
                while _hash_code(code) in existing_hashes:
                    code = _generate_code()
                code_hash = _hash_code(code)
                existing_hashes.add(code_hash)
                item = {
                    "id": f"cdk_{uuid.uuid4().hex[:12]}",
                    "code_hash": code_hash,
                    "code_prefix": _normalize_code(code)[:4],
                    "name": _clean(name) or "未命名 CDK",
                    "type": cdk_type,
                    "quota": _parse_int(quota),
                    "package_id": package.get("id") if package else None,
                    "package_name": package.get("name") if package else None,
                    "max_redemptions": max(1, int(max_redemptions or 1)),
                    "redeemed_count": 0,
                    "per_user_limit": max(1, int(per_user_limit or 1)),
                    "enabled": True,
                    "starts_at": None,
                    "expires_at": _clean(expires_at) or None,
                    "created_at": now,
                    "updated_at": now,
                    "last_redeemed_at": None,
                }
                self._cdks.append(item)
                created.append(self._public_cdk(item))
                raw_codes.append(code)
            self._save_cdks()
        return {"items": self.list_cdks(), "created": created, "codes": raw_codes}

    def update_cdk(self, cdk_id: str, updates: dict[str, object]) -> dict[str, object] | None:
        with self._lock:
            for index, item in enumerate(self._cdks):
                if item.get("id") != cdk_id:
                    continue
                next_item = dict(item)
                if "name" in updates and updates.get("name") is not None:
                    next_item["name"] = _clean(updates.get("name"))
                if "enabled" in updates and updates.get("enabled") is not None:
                    next_item["enabled"] = bool(updates.get("enabled"))
                if "expires_at" in updates:
                    next_item["expires_at"] = _clean(updates.get("expires_at")) or None
                next_item["updated_at"] = _now_iso()
                self._cdks[index] = next_item
                self._save_cdks()
                return self._public_cdk(next_item)
        return None

    def list_redemptions(self) -> list[dict[str, object]]:
        with self._lock:
            return [dict(item) for item in sorted(self._redemptions, key=lambda item: str(item.get("redeemed_at") or ""), reverse=True)]

    def redeem(self, code: str, identity: dict[str, object]) -> dict[str, object]:
        user_id = _clean(identity.get("user_id"))
        if not user_id:
            raise ValueError("registered user is required")
        code_hash = _hash_code(code)
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        with self._lock:
            cdk_index = None
            cdk = None
            for index, item in enumerate(self._cdks):
                if item.get("code_hash") == code_hash:
                    cdk_index = index
                    cdk = item
                    break
            if cdk is None or cdk_index is None:
                raise ValueError("cdk is invalid")
            if not bool(cdk.get("enabled", True)):
                raise ValueError("cdk is disabled")
            starts_at = _parse_optional_datetime(cdk.get("starts_at"))
            expires_at = _parse_optional_datetime(cdk.get("expires_at"))
            if starts_at and starts_at > now_dt:
                raise ValueError("cdk is not active")
            if expires_at and expires_at <= now_dt:
                raise ValueError("cdk is expired")
            if _parse_int(cdk.get("redeemed_count")) >= max(1, _parse_int(cdk.get("max_redemptions"), 1)):
                raise ValueError("cdk has been fully redeemed")
            user_redeems = [item for item in self._redemptions if item.get("cdk_id") == cdk.get("id") and item.get("user_id") == user_id]
            if len(user_redeems) >= max(1, _parse_int(cdk.get("per_user_limit"), 1)):
                raise ValueError("cdk redemption limit reached")
            quota = _parse_int(cdk.get("quota"))
            package_id = _clean(cdk.get("package_id")) or None
            package_name = _clean(cdk.get("package_name")) or None
            package_expires_at = None
            if cdk.get("type") == "package" and package_id:
                package = next((item for item in self._packages if item.get("id") == package_id and bool(item.get("enabled", True))), None)
                if package is None:
                    raise ValueError("package is invalid")
                quota = _parse_int(package.get("quota"))
                package_name = _clean(package.get("name"))
                valid_days = _parse_int(package.get("valid_days")) if package.get("valid_days") is not None else None
                if valid_days:
                    package_expires_at = (now_dt + timedelta(days=valid_days)).isoformat()
            user = auth_service.adjust_user_quota(
                user_id,
                quota,
                "cdk",
                ref_type="cdk",
                ref_id=str(cdk.get("id") or ""),
                metadata={"cdk_type": str(cdk.get("type") or ""), "code_prefix": str(cdk.get("code_prefix") or "")},
            )
            if user is None:
                raise ValueError("user is invalid")
            if package_id:
                user = auth_service.update_user(user_id, {
                    "package_id": package_id,
                    "package_name": package_name,
                    "package_expires_at": package_expires_at,
                })
            next_cdk = dict(cdk)
            next_cdk["redeemed_count"] = _parse_int(next_cdk.get("redeemed_count")) + 1
            next_cdk["last_redeemed_at"] = now
            next_cdk["updated_at"] = now
            self._cdks[cdk_index] = next_cdk
            record = {
                "id": f"red_{uuid.uuid4().hex[:12]}",
                "cdk_id": next_cdk.get("id"),
                "code_prefix": next_cdk.get("code_prefix"),
                "user_id": user_id,
                "email": identity.get("email"),
                "type": next_cdk.get("type"),
                "quota_granted": quota,
                "package_id": package_id,
                "package_name": package_name,
                "redeemed_at": now,
            }
            self._redemptions.append(record)
            self._save_cdks()
            self._save_redemptions()
            return {"grant": record, "user": user, "cdk": self._public_cdk(next_cdk)}


redemption_service = RedemptionService(config.get_storage_backend())
