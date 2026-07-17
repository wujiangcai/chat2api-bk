from __future__ import annotations

import time
from pathlib import Path
from threading import Event, Lock, Thread

from fastapi import HTTPException, Request

from services.account_service import account_service
from services.auth_service import auth_service
from services.config import config
from services.rate_limit_service import create_rate_limiter_from_env

BASE_DIR = Path(__file__).resolve().parents[1]
WEB_DIST_DIR = BASE_DIR / "web_dist"

_rate_limit_lock = Lock()
_recent_usage: dict[str, list[float]] = {}
_api_rate_limiter = create_rate_limiter_from_env(namespace="api-keys", memory_bucket=_recent_usage)


def extract_bearer_token(authorization: str | None) -> str:
    scheme, _, value = str(authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return ""
    return value.strip()


def _legacy_admin_identity(token: str) -> dict[str, object] | None:
    auth_key = str(config.auth_key or "").strip()
    if auth_key and token == auth_key:
        return {"id": "admin", "name": "管理员", "role": "admin", "permissions": ["*"]}
    return None


def require_identity(authorization: str | None) -> dict[str, object]:
    token = extract_bearer_token(authorization)
    identity = _legacy_admin_identity(token) or auth_service.authenticate(token)
    if identity is None:
        raise HTTPException(status_code=401, detail={"error": "authorization is invalid"})
    if auth_service.is_expired(identity):
        raise HTTPException(status_code=403, detail={"error": "authorization is expired"})
    return identity


def require_auth_key(authorization: str | None) -> None:
    require_identity(authorization)


def require_admin(authorization: str | None) -> dict[str, object]:
    identity = require_identity(authorization)
    if identity.get("role") != "admin":
        raise HTTPException(status_code=403, detail={"error": "admin permission required"})
    return identity


def require_permission(authorization: str | None, permission: str) -> dict[str, object]:
    identity = require_identity(authorization)
    if not auth_service.has_permission(identity, permission):
        raise HTTPException(status_code=403, detail={"error": f"permission required: {permission}"})
    return identity


def check_quota(identity: dict[str, object], units: int) -> None:
    if not auth_service.check_quota(identity, units):
        raise HTTPException(
            status_code=429,
            detail={
                "error": "quota exceeded",
                "quota_limit": identity.get("quota_limit"),
                "quota_used": identity.get("quota_used"),
                "quota_remaining": identity.get("quota_remaining"),
                "quota_balance": identity.get("quota_balance"),
            },
        )


def reserve_quota(identity: dict[str, object], units: int) -> int:
    if identity.get("role") == "admin":
        return 0
    user_id = str(identity.get("user_id") or "").strip()
    requested = max(1, int(units or 1))
    if user_id:
        if not auth_service.try_consume_user_quota(user_id, requested):
            raise HTTPException(
                status_code=429,
                detail={"error": "quota exceeded", "quota_balance": identity.get("quota_balance")},
            )
        return requested
    check_quota(identity, requested)
    return 0


def refund_quota(identity: dict[str, object], units: int) -> None:
    user_id = str(identity.get("user_id") or "").strip()
    if user_id and units > 0:
        auth_service.refund_user_quota(user_id, units)


def consume_quota(identity: dict[str, object], units: int) -> None:
    if identity.get("role") == "admin":
        return
    user_id = str(identity.get("user_id") or "").strip()
    if user_id:
        return
    auth_service.consume_quota(str(identity.get("id") or ""), units)


def check_rate_limit(identity: dict[str, object], units: int = 1) -> None:
    if identity.get("role") == "admin":
        return
    raw_limit = identity.get("rate_limit_per_minute")
    if raw_limit is None:
        return
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        return
    if limit <= 0:
        return
    key = str(identity.get("id") or identity.get("name") or "unknown")
    requested = max(1, int(units or 1))
    result = _api_rate_limiter.allow(key, limit, window_seconds=60, cost=requested)
    if not result.allowed:
        raise HTTPException(
            status_code=429,
            detail={"error": f"rate limit exceeded: max {limit} unit(s) per minute"},
            headers={"Retry-After": str(result.retry_after_seconds or 60)},
        )


def count_success_items(result: object, fallback: int) -> int:
    if not isinstance(result, dict):
        return max(0, int(fallback or 0))
    items = result.get("data")
    if not isinstance(items, list):
        return 0 if result.get("error") else max(0, int(fallback or 0))
    count = sum(1 for item in items if isinstance(item, dict) and (item.get("b64_json") or item.get("url")) and not item.get("error"))
    return count if count > 0 else 0


def resolve_image_base_url(request: Request) -> str:
    return config.base_url or f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"


def raise_image_quota_error(exc: Exception) -> None:
    message = str(exc)
    if "no available image quota" in message.lower():
        raise HTTPException(status_code=429, detail={"error": "no available image quota"}) from exc
    raise HTTPException(status_code=502, detail={"error": message}) from exc


def sanitize_cpa_pool(pool: dict | None) -> dict | None:
    if not isinstance(pool, dict):
        return None
    return {key: value for key, value in pool.items() if key != "secret_key"}


def sanitize_cpa_pools(pools: list[dict]) -> list[dict]:
    return [sanitized for pool in pools if (sanitized := sanitize_cpa_pool(pool)) is not None]


def sanitize_sub2api_server(server: dict | None) -> dict | None:
    if not isinstance(server, dict):
        return None
    sanitized = {key: value for key, value in server.items() if key not in {"password", "api_key"}}
    sanitized["has_api_key"] = bool(str(server.get("api_key") or "").strip())
    return sanitized


def sanitize_sub2api_servers(servers: list[dict]) -> list[dict]:
    return [sanitized for server in servers if (sanitized := sanitize_sub2api_server(server)) is not None]


def start_limited_account_watcher(stop_event: Event) -> Thread:
    interval_seconds = config.refresh_account_interval_minute * 60

    def worker() -> None:
        while not stop_event.is_set():
            try:
                limited_tokens = account_service.list_limited_tokens()
                if limited_tokens:
                    print(f"[account-limited-watcher] checking {len(limited_tokens)} limited accounts")
                    account_service.refresh_accounts(limited_tokens)
            except Exception as exc:
                print(f"[account-limited-watcher] fail {exc}")
            stop_event.wait(interval_seconds)

    thread = Thread(target=worker, name="limited-account-watcher", daemon=True)
    thread.start()
    return thread


def resolve_web_asset(requested_path: str) -> Path | None:
    if not WEB_DIST_DIR.exists():
        return None
    clean_path = requested_path.strip("/")
    base_dir = WEB_DIST_DIR.resolve()
    candidates = [base_dir / "index.html"] if not clean_path else [
        base_dir / Path(clean_path),
        base_dir / clean_path / "index.html",
        base_dir / f"{clean_path}.html",
    ]
    for candidate in candidates:
        try:
            candidate.resolve().relative_to(base_dir)
        except ValueError:
            continue
        if candidate.is_file():
            return candidate
    return None
