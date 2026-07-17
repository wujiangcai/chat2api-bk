from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote


def _clean(value: object) -> str:
    return str(value or "").strip()


def _normalize_key(key: object, prefix: str = "") -> str:
    clean_key = _clean(key).replace("\\", "/").lstrip("/")
    clean_prefix = _clean(prefix).replace("\\", "/").strip("/")
    if clean_prefix:
        clean_key = f"{clean_prefix}/{clean_key}" if clean_key else clean_prefix
    if not clean_key or ".." in clean_key.split("/"):
        raise ValueError("object key is invalid")
    return clean_key


def _quote_key(key: str) -> str:
    return "/".join(quote(part) for part in key.split("/"))


class ObjectStorage(Protocol):
    backend: str

    def put_object(self, key: str, data: bytes, content_type: str) -> str:
        ...

    def delete_object(self, key: str) -> None:
        ...

    def public_url(self, key: str, *, base_url: str = "") -> str:
        ...

    def info(self) -> dict[str, object]:
        ...


class LocalObjectStorage:
    backend = "local"

    def __init__(self, root_dir: Path, *, route_prefix: str = "/assets"):
        self.root_dir = root_dir
        self.route_prefix = "/" + route_prefix.strip("/")
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        normalized_key = _normalize_key(key)
        root = self.root_dir.resolve()
        path = (root / normalized_key).resolve()
        path.relative_to(root)
        return path

    def put_object(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        normalized_key = _normalize_key(key)
        path = self._path(normalized_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return normalized_key

    def delete_object(self, key: str) -> None:
        try:
            self._path(key).unlink(missing_ok=True)
        except FileNotFoundError:
            return

    def public_url(self, key: str, *, base_url: str = "") -> str:
        normalized_key = _normalize_key(key)
        path = f"{self.route_prefix}/{_quote_key(normalized_key)}"
        return f"{base_url.rstrip('/')}{path}" if base_url else path

    def info(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "root_dir": str(self.root_dir),
            "route_prefix": self.route_prefix,
        }


class S3ObjectStorage:
    backend = "s3"

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str = "",
        region_name: str = "",
        access_key_id: str = "",
        secret_access_key: str = "",
        public_base_url: str = "",
        key_prefix: str = "",
        client: Any | None = None,
        extra_args: dict[str, object] | None = None,
    ):
        self.bucket = _clean(bucket)
        if not self.bucket:
            raise ValueError("OBJECT_STORAGE_BUCKET is required for S3 object storage")
        self.endpoint_url = _clean(endpoint_url).rstrip("/")
        self.region_name = _clean(region_name) or "auto"
        self.public_base_url = _clean(public_base_url).rstrip("/")
        self.key_prefix = _clean(key_prefix)
        self.extra_args = dict(extra_args or {})
        self.client = client or self._create_client(access_key_id, secret_access_key)

    def _create_client(self, access_key_id: str, secret_access_key: str):
        try:
            import boto3  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError("S3/R2/MinIO object storage requires boto3. Install boto3 before enabling OBJECT_STORAGE_BACKEND=s3.") from exc

        return boto3.client(
            "s3",
            endpoint_url=self.endpoint_url or None,
            region_name=None if self.region_name == "auto" else self.region_name,
            aws_access_key_id=_clean(access_key_id) or None,
            aws_secret_access_key=_clean(secret_access_key) or None,
        )

    def put_object(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        normalized_key = _normalize_key(key, self.key_prefix)
        kwargs = {
            "Bucket": self.bucket,
            "Key": normalized_key,
            "Body": data,
            "ContentType": content_type or "application/octet-stream",
        }
        kwargs.update(self.extra_args)
        self.client.put_object(**kwargs)
        return normalized_key

    def delete_object(self, key: str) -> None:
        normalized_key = _normalize_key(key)
        self.client.delete_object(Bucket=self.bucket, Key=normalized_key)

    def public_url(self, key: str, *, base_url: str = "") -> str:
        normalized_key = _normalize_key(key)
        quoted_key = _quote_key(normalized_key)
        if self.public_base_url:
            return f"{self.public_base_url}/{quoted_key}"
        if self.endpoint_url:
            return f"{self.endpoint_url}/{self.bucket}/{quoted_key}"
        region = "" if self.region_name == "auto" else f".{self.region_name}"
        return f"https://{self.bucket}.s3{region}.amazonaws.com/{quoted_key}"

    def info(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "bucket": self.bucket,
            "endpoint_url": self.endpoint_url,
            "region_name": self.region_name,
            "public_base_url": self.public_base_url,
            "key_prefix": self.key_prefix,
        }


def create_object_storage_from_env(default_local_dir: Path) -> ObjectStorage:
    backend = _clean(os.getenv("OBJECT_STORAGE_BACKEND") or "local").lower()
    if backend in {"", "local", "filesystem", "fs"}:
        return LocalObjectStorage(default_local_dir)
    if backend in {"s3", "r2", "minio", "oss", "cos"}:
        acl = _clean(os.getenv("OBJECT_STORAGE_ACL"))
        extra_args: dict[str, object] = {}
        if acl:
            extra_args["ACL"] = acl
        return S3ObjectStorage(
            bucket=os.getenv("OBJECT_STORAGE_BUCKET", ""),
            endpoint_url=os.getenv("OBJECT_STORAGE_ENDPOINT", ""),
            region_name=os.getenv("OBJECT_STORAGE_REGION", ""),
            access_key_id=os.getenv("OBJECT_STORAGE_ACCESS_KEY_ID", ""),
            secret_access_key=os.getenv("OBJECT_STORAGE_SECRET_ACCESS_KEY", ""),
            public_base_url=os.getenv("OBJECT_STORAGE_PUBLIC_BASE_URL", ""),
            key_prefix=os.getenv("OBJECT_STORAGE_PREFIX", ""),
            extra_args=extra_args,
        )
    raise ValueError(f"Unknown OBJECT_STORAGE_BACKEND: {backend}. Supported: local, s3, r2, minio")
