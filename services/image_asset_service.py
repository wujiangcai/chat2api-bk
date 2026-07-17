from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import unquote, urlparse

from PIL import Image

from services.config import config
from services.object_storage import LocalObjectStorage, ObjectStorage
from services.storage.base import StorageBackend


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(value: object) -> str:
    return str(value or "").strip()


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _date_from_iso(value: object) -> str:
    text = _clean(value)
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except ValueError:
        return text[:10]


class ImageAssetService:
    """Image asset metadata + object storage.

    This keeps large image bytes out of the metadata collection and gives the
    product a stable gallery/asset layer. The storage path is intentionally
    object-key based so local development and S3/R2/MinIO production share the
    same API response shape.
    """

    def __init__(
        self,
        storage: StorageBackend,
        assets_dir: Path | None = None,
        object_storage: ObjectStorage | None = None,
    ):
        self.storage = storage
        if object_storage is not None:
            self.object_storage = object_storage
        elif assets_dir is not None:
            self.object_storage = LocalObjectStorage(assets_dir)
        else:
            self.object_storage = config.get_object_storage_backend()
        self._lock = Lock()
        self._assets = self._load_assets()

    @staticmethod
    def new_asset_id() -> str:
        return f"asset_{uuid.uuid4().hex[:16]}"

    @staticmethod
    def _normalize_owner(raw: object) -> dict[str, object]:
        if not isinstance(raw, dict):
            raw = {}
        return {
            "role": _clean(raw.get("role")) or "user",
            "key_id": _clean(raw.get("key_id") or raw.get("id")) or None,
            "key_name": _clean(raw.get("key_name") or raw.get("name")) or None,
            "user_id": _clean(raw.get("user_id")) or None,
            "email": _clean(raw.get("email")) or None,
        }

    def _normalize_asset(self, raw: object) -> dict[str, object] | None:
        if not isinstance(raw, dict):
            return None
        asset_id = _clean(raw.get("id"))
        if not asset_id:
            return None
        created_at = _clean(raw.get("created_at")) or _now_iso()
        return {
            "id": asset_id,
            "owner": self._normalize_owner(raw.get("owner")),
            "job_id": _clean(raw.get("job_id")) or None,
            "source": _clean(raw.get("source")) or "image.generation",
            "model": _clean(raw.get("model")) or None,
            "prompt_hash": _clean(raw.get("prompt_hash")) or None,
            "prompt_preview": _clean(raw.get("prompt_preview")) or "",
            "object_key": _clean(raw.get("object_key")) or None,
            "storage_backend": _clean(raw.get("storage_backend")) or None,
            "mime_type": _clean(raw.get("mime_type")) or "image/png",
            "size_bytes": max(0, _safe_int(raw.get("size_bytes"), 0)),
            "width": max(0, _safe_int(raw.get("width"), 0)),
            "height": max(0, _safe_int(raw.get("height"), 0)),
            "status": _clean(raw.get("status")) or "active",
            "revised_prompt": _clean(raw.get("revised_prompt")) or None,
            "created_at": created_at,
            "deleted_at": _clean(raw.get("deleted_at")) or None,
        }

    def _load_assets(self) -> list[dict[str, object]]:
        try:
            items = self.storage.load_collection("image_assets")
        except Exception:
            return []
        if not isinstance(items, list):
            return []
        assets = [normalized for item in items if (normalized := self._normalize_asset(item)) is not None]
        assets.sort(key=lambda item: str(item.get("created_at") or ""))
        return assets

    def _save_asset(self, asset: dict[str, object]) -> None:
        self.storage.append_collection_item("image_assets", asset)

    @staticmethod
    def _can_access(identity: dict[str, object], asset: dict[str, object]) -> bool:
        if identity.get("role") == "admin":
            return True
        owner = asset.get("owner") if isinstance(asset.get("owner"), dict) else {}
        user_id = _clean(identity.get("user_id"))
        key_id = _clean(identity.get("key_id") or identity.get("id"))
        return bool(
            (user_id and owner.get("user_id") == user_id)
            or (key_id and owner.get("key_id") == key_id)
        )

    @staticmethod
    def _public_url(object_key: object, base_url: str = "") -> str:
        # Kept for backwards compatibility with old tests/callers; the
        # instance method below is used by current code so S3/R2 URLs are
        # generated by the active object storage backend.
        key = _clean(object_key).replace("\\", "/").lstrip("/")
        return f"{base_url.rstrip('/')}/assets/{key}" if base_url else f"/assets/{key}"

    def _public_asset(self, asset: dict[str, object], *, base_url: str = "") -> dict[str, object]:
        object_key = _clean(asset.get("object_key"))
        created_date = _date_from_iso(asset.get("created_at"))
        return {
            "id": asset.get("id"),
            "owner": dict(asset.get("owner") or {}),
            "job_id": asset.get("job_id"),
            "source": asset.get("source"),
            "model": asset.get("model"),
            "prompt_hash": asset.get("prompt_hash"),
            "prompt_preview": asset.get("prompt_preview"),
            "object_key": object_key or None,
            "storage_backend": asset.get("storage_backend"),
            "url": self.object_storage.public_url(object_key, base_url=base_url) if object_key else "",
            "mime_type": asset.get("mime_type"),
            "size_bytes": asset.get("size_bytes"),
            "width": asset.get("width"),
            "height": asset.get("height"),
            "status": asset.get("status"),
            "revised_prompt": asset.get("revised_prompt"),
            "created_at": asset.get("created_at"),
            "deleted_at": asset.get("deleted_at"),
            # Compatibility aliases for the existing image manager shape.
            "name": Path(object_key).name if object_key else str(asset.get("id") or ""),
            "date": created_date,
            "size": asset.get("size_bytes"),
        }

    @staticmethod
    def _image_dimensions(data: bytes) -> tuple[int, int]:
        try:
            with Image.open(BytesIO(data)) as image:
                return image.size
        except Exception:
            return 0, 0

    @staticmethod
    def _decode_b64(value: object) -> bytes | None:
        text = _clean(value)
        if not text:
            return None
        try:
            return base64.b64decode(text)
        except Exception:
            return None

    @staticmethod
    def _read_local_image_url(url: object) -> bytes | None:
        text = _clean(url)
        if not text:
            return None
        parsed = urlparse(text)
        path = unquote(parsed.path if parsed.scheme else text)
        marker = "/images/"
        if marker not in path:
            return None
        rel = path.split(marker, 1)[1].lstrip("/")
        try:
            root = config.images_dir.resolve()
            candidate = (root / rel).resolve()
            candidate.relative_to(root)
        except Exception:
            return None
        if not candidate.is_file():
            return None
        try:
            return candidate.read_bytes()
        except Exception:
            return None

    def _bytes_from_item(self, item: dict[str, object]) -> bytes | None:
        return self._decode_b64(item.get("b64_json")) or self._read_local_image_url(item.get("url"))

    def archive_result(
        self,
        *,
        owner: dict[str, object],
        result: object,
        job_id: str | None = None,
        source: str = "image.generation",
        model: str = "",
        prompt: str = "",
        base_url: str = "",
    ) -> list[dict[str, object]]:
        if not isinstance(result, dict) or not isinstance(result.get("data"), list):
            return []

        owner_data = self._normalize_owner(owner)
        prompt_text = _clean(prompt)
        prompt_hash = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest() if prompt_text else None
        prompt_preview = prompt_text[:120]
        created = _now_iso()
        day = datetime.now(timezone.utc).strftime("%Y/%m/%d")
        archived: list[dict[str, object]] = []

        for item in result.get("data") or []:
            if not isinstance(item, dict) or item.get("error"):
                continue
            image_data = self._bytes_from_item(item)
            if not image_data:
                continue
            asset_id = self.new_asset_id()
            object_key = f"{day}/{asset_id}.png"
            stored_key = self.object_storage.put_object(object_key, image_data, "image/png")
            width, height = self._image_dimensions(image_data)
            asset = {
                "id": asset_id,
                "owner": owner_data,
                "job_id": _clean(job_id) or None,
                "source": _clean(source) or "image.generation",
                "model": _clean(model) or None,
                "prompt_hash": prompt_hash,
                "prompt_preview": prompt_preview,
                "object_key": stored_key,
                "storage_backend": getattr(self.object_storage, "backend", "unknown"),
                "mime_type": "image/png",
                "size_bytes": len(image_data),
                "width": width,
                "height": height,
                "status": "active",
                "revised_prompt": _clean(item.get("revised_prompt")) or None,
                "created_at": created,
                "deleted_at": None,
            }
            with self._lock:
                self._assets.append(asset)
                self._save_asset(asset)
            archived.append(self._public_asset(asset, base_url=base_url))
        return archived

    def list_assets(
        self,
        identity: dict[str, object] | None = None,
        *,
        base_url: str = "",
        limit: int = 100,
        start_date: str = "",
        end_date: str = "",
        include_deleted: bool = False,
    ) -> list[dict[str, object]]:
        safe_limit = min(max(1, int(limit or 100)), 1000)
        with self._lock:
            items = list(self._assets)
            if identity is not None and identity.get("role") != "admin":
                items = [asset for asset in items if self._can_access(identity, asset)]
            if not include_deleted:
                items = [asset for asset in items if asset.get("status") != "deleted"]
            if start_date:
                items = [asset for asset in items if _date_from_iso(asset.get("created_at")) >= start_date]
            if end_date:
                items = [asset for asset in items if _date_from_iso(asset.get("created_at")) <= end_date]
            items.sort(key=lambda asset: str(asset.get("created_at") or ""), reverse=True)
            return [self._public_asset(asset, base_url=base_url) for asset in items[:safe_limit]]

    def get_asset(self, asset_id: str, identity: dict[str, object] | None = None, *, base_url: str = "") -> dict[str, object] | None:
        normalized_id = _clean(asset_id)
        with self._lock:
            for asset in self._assets:
                if asset.get("id") != normalized_id:
                    continue
                if identity is not None and not self._can_access(identity, asset):
                    return None
                return self._public_asset(asset, base_url=base_url)
        return None

    def delete_asset(self, asset_id: str, identity: dict[str, object]) -> dict[str, object] | None:
        normalized_id = _clean(asset_id)
        with self._lock:
            for index, asset in enumerate(self._assets):
                if asset.get("id") != normalized_id or not self._can_access(identity, asset):
                    continue
                next_asset = dict(asset)
                next_asset["status"] = "deleted"
                next_asset["deleted_at"] = _now_iso()
                object_key = _clean(next_asset.get("object_key"))
                if object_key:
                    try:
                        self.object_storage.delete_object(object_key)
                    except Exception:
                        pass
                self._assets[index] = next_asset
                self._save_asset(next_asset)
                return self._public_asset(next_asset)
        return None


image_asset_service = ImageAssetService(config.get_storage_backend())
