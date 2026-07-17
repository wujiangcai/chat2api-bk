from __future__ import annotations

from dataclasses import dataclass
import json
import os
import sys
from pathlib import Path
import time

from services.storage.base import StorageBackend

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
CONFIG_FILE = BASE_DIR / "config.json"
VERSION_FILE = BASE_DIR / "VERSION"


@dataclass(frozen=True)
class LoadedSettings:
    auth_key: str
    refresh_account_interval_minute: int


def _normalize_auth_key(value: object) -> str:
    return str(value or "").strip()


def _is_invalid_auth_key(value: object) -> bool:
    return _normalize_auth_key(value) == ""


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _read_json_object(path: Path, *, name: str) -> dict[str, object]:
    if not path.exists():
        return {}
    if path.is_dir():
        print(
            f"Warning: {name} at '{path}' is a directory, ignoring it and falling back to other configuration sources.",
            file=sys.stderr,
        )
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _load_settings() -> LoadedSettings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    raw_config = _read_json_object(CONFIG_FILE, name="config.json")
    auth_key = _normalize_auth_key(os.getenv("CHATGPT2API_AUTH_KEY") or raw_config.get("auth-key"))
    if _is_invalid_auth_key(auth_key):
        raise ValueError(
            "❌ auth-key 未设置！\n"
            "请在环境变量 CHATGPT2API_AUTH_KEY 中设置，或者在 config.json 中填写 auth-key。"
        )

    try:
        refresh_interval = int(raw_config.get("refresh_account_interval_minute", 5))
    except (TypeError, ValueError):
        refresh_interval = 5

    return LoadedSettings(
        auth_key=auth_key,
        refresh_account_interval_minute=refresh_interval,
    )


class ConfigStore:
    def __init__(self, path: Path):
        self.path = path
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.data = self._load()
        self._storage_backend: StorageBackend | None = None
        self._object_storage_backend = None
        if _is_invalid_auth_key(self.auth_key):
            raise ValueError(
                "❌ auth-key 未设置！\n"
                "请按以下任意一种方式解决：\n"
                "1. 在 Render 的 Environment 变量中添加：\n"
                "   CHATGPT2API_AUTH_KEY = your_real_auth_key\n"
                "2. 或者在 config.json 中填写：\n"
                '   "auth-key": "your_real_auth_key"'
            )

    def _load(self) -> dict[str, object]:
        return _read_json_object(self.path, name="config.json")

    def _save(self) -> None:
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    @property
    def auth_key(self) -> str:
        return _normalize_auth_key(os.getenv("CHATGPT2API_AUTH_KEY") or self.data.get("auth-key"))

    @property
    def accounts_file(self) -> Path:
        return DATA_DIR / "accounts.json"

    @property
    def refresh_account_interval_minute(self) -> int:
        try:
            return int(self.data.get("refresh_account_interval_minute", 5))
        except (TypeError, ValueError):
            return 5

    @property
    def image_retention_days(self) -> int:
        try:
            return max(1, int(self.data.get("image_retention_days", 30)))
        except (TypeError, ValueError):
            return 30

    @property
    def auto_remove_invalid_accounts(self) -> bool:
        value = self.data.get("auto_remove_invalid_accounts", False)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @property
    def auto_disable_consecutive_fail(self) -> int:
        """连续失败多少次后自动禁用账号；0 表示关闭。默认 5。"""
        try:
            value = int(self.data.get("auto_disable_consecutive_fail", 5))
        except (TypeError, ValueError):
            value = 5
        return max(0, value)

    @property
    def images_dir(self) -> Path:
        path = DATA_DIR / "images"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def assets_dir(self) -> Path:
        path = DATA_DIR / "assets"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def cleanup_old_images(self) -> int:
        cutoff = time.time() - self.image_retention_days * 86400
        removed = 0
        for path in self.images_dir.rglob("*"):
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        for path in sorted((p for p in self.images_dir.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            try:
                path.rmdir()
            except OSError:
                pass
        return removed

    @property
    def base_url(self) -> str:
        return str(
            os.getenv("CHATGPT2API_BASE_URL")
            or self.data.get("base_url")
            or ""
        ).strip().rstrip("/")

    @property
    def app_version(self) -> str:
        try:
            value = VERSION_FILE.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return "0.0.0"
        return value or "0.0.0"

    @property
    def web_allowed_origins(self) -> list[str]:
        raw = os.getenv("WEB_ALLOWED_ORIGINS") or self.data.get("web_allowed_origins") or self.data.get("allowed_origins") or ""
        if isinstance(raw, list):
            values = raw
        else:
            values = str(raw or "").split(",")
        origins = []
        for value in values:
            origin = str(value or "").strip().rstrip("/")
            if origin and origin not in origins:
                origins.append(origin)
        if origins:
            return origins
        if self.is_production:
            return []
        return ["http://127.0.0.1:3000", "http://localhost:3000"]

    @property
    def is_production(self) -> bool:
        raw = os.getenv("APP_ENV") or os.getenv("ENVIRONMENT") or os.getenv("NODE_ENV") or self.data.get("app_env") or ""
        return str(raw).strip().lower() in {"prod", "production"}

    @property
    def security_headers_enabled(self) -> bool:
        return _env_bool("SECURITY_HEADERS_ENABLED", True)

    @property
    def hsts_enabled(self) -> bool:
        default = self.is_production or self.base_url.startswith("https://")
        return _env_bool("ENABLE_HSTS", default)

    @property
    def hsts_max_age_seconds(self) -> int:
        try:
            return max(0, int(os.getenv("HSTS_MAX_AGE_SECONDS") or self.data.get("hsts_max_age_seconds") or 31536000))
        except (TypeError, ValueError):
            return 31536000

    @property
    def force_https(self) -> bool:
        return _env_bool("FORCE_HTTPS", False)

    @property
    def content_security_policy(self) -> str:
        configured = str(os.getenv("CONTENT_SECURITY_POLICY") or self.data.get("content_security_policy") or "").strip()
        if configured:
            return configured
        return (
            "default-src 'self'; "
            "base-uri 'self'; "
            "object-src 'none'; "
            "frame-ancestors 'none'; "
            "img-src 'self' data: blob: https:; "
            "font-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
            "connect-src 'self' https: ws: wss:"
        )

    def get(self) -> dict[str, object]:
        data = dict(self.data)
        data["refresh_account_interval_minute"] = self.refresh_account_interval_minute
        data["image_retention_days"] = self.image_retention_days
        data["auto_remove_invalid_accounts"] = self.auto_remove_invalid_accounts
        data["web_allowed_origins"] = self.web_allowed_origins
        data["security_headers_enabled"] = self.security_headers_enabled
        data["hsts_enabled"] = self.hsts_enabled
        data["force_https"] = self.force_https
        data.pop("auth-key", None)
        return data

    def get_proxy_settings(self) -> str:
        return str(self.data.get("proxy") or "").strip()

    def update(self, data: dict[str, object]) -> dict[str, object]:
        next_data = dict(self.data)
        next_data.update(dict(data or {}))
        self.data = next_data
        self._save()
        return self.get()

    def get_storage_backend(self) -> StorageBackend:
        """获取存储后端实例（单例）"""
        if self._storage_backend is None:
            from services.storage.factory import create_storage_backend
            self._storage_backend = create_storage_backend(DATA_DIR)
        return self._storage_backend

    def get_object_storage_backend(self):
        """获取对象存储后端实例（单例）"""
        if self._object_storage_backend is None:
            from services.object_storage import create_object_storage_from_env
            self._object_storage_backend = create_object_storage_from_env(self.assets_dir)
        return self._object_storage_backend


config = ConfigStore(CONFIG_FILE)
