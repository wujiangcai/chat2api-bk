from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from services.auth_service import auth_service
from services.log_service import LOG_TYPE_ACCOUNT, log_service
from services.redemption_service import redemption_service

from api.support import (
    require_admin,
    sanitize_cpa_pool,
    sanitize_cpa_pools,
    sanitize_sub2api_server,
    sanitize_sub2api_servers,
)
from services.account_service import account_service
from services.cpa_service import cpa_config, cpa_import_service, list_remote_files
from services.sub2api_service import (
    list_remote_accounts as sub2api_list_remote_accounts,
    list_remote_groups as sub2api_list_remote_groups,
    sub2api_config,
    sub2api_import_service,
)



class UserKeyCreateRequest(BaseModel):
    name: str = ""
    permissions: list[str] | None = None
    quota_limit: int | None = Field(default=None, ge=0)
    rate_limit_per_minute: int | None = Field(default=None, ge=0)
    expires_at: str | None = None
    metadata: dict[str, object] | None = None


class UserKeyUpdateRequest(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    permissions: list[str] | None = None
    quota_limit: int | None = Field(default=None, ge=0)
    quota_unlimited: bool = False
    rate_limit_per_minute: int | None = Field(default=None, ge=0)
    rate_limit_unlimited: bool = False
    expires_at: str | None = None
    expires_never: bool = False
    metadata: dict[str, object] | None = None
    reset_quota_used: bool = False
    add_quota: int | None = Field(default=None, ge=0)


class AccountCreateRequest(BaseModel):
    tokens: list[str] = Field(default_factory=list)


class AccountDeleteRequest(BaseModel):
    tokens: list[str] = Field(default_factory=list)


class AccountRefreshRequest(BaseModel):
    access_tokens: list[str] = Field(default_factory=list)


class AccountUpdateRequest(BaseModel):
    access_token: str = ""
    type: str | None = None
    status: str | None = None
    quota: int | None = None
    disabled: bool | None = None
    reset_consecutive_fail: bool = False


class CPAPoolCreateRequest(BaseModel):
    name: str = ""
    base_url: str = ""
    secret_key: str = ""


class CPAPoolUpdateRequest(BaseModel):
    name: str | None = None
    base_url: str | None = None
    secret_key: str | None = None


class CPAImportRequest(BaseModel):
    names: list[str] = Field(default_factory=list)


class Sub2APIServerCreateRequest(BaseModel):
    name: str = ""
    base_url: str = ""
    email: str = ""
    password: str = ""
    api_key: str = ""
    group_id: str = ""


class Sub2APIServerUpdateRequest(BaseModel):
    name: str | None = None
    base_url: str | None = None
    email: str | None = None
    password: str | None = None
    api_key: str | None = None
    group_id: str | None = None


class Sub2APIImportRequest(BaseModel):
    account_ids: list[str] = Field(default_factory=list)


class AdminUserCreateRequest(BaseModel):
    email: str = ""
    password: str = Field(..., min_length=8)
    name: str = ""
    quota_balance: int = Field(default=0, ge=0)


class AdminUserUpdateRequest(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    quota_balance: int | None = Field(default=None, ge=0)
    package_id: str | None = None
    package_name: str | None = None
    package_expires_at: str | None = None


class AdminUserPasswordRequest(BaseModel):
    password: str = Field(..., min_length=8)


class AdminUserQuotaRequest(BaseModel):
    delta: int
    reason: str = ""


class PackageCreateRequest(BaseModel):
    name: str = ""
    description: str = ""
    quota: int = Field(default=0, ge=0)
    price_cents: int = Field(default=0, ge=0)
    currency: str = "CNY"
    valid_days: int | None = Field(default=None, ge=0)


class PackageUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    quota: int | None = Field(default=None, ge=0)
    price_cents: int | None = Field(default=None, ge=0)
    currency: str | None = None
    valid_days: int | None = Field(default=None, ge=0)
    enabled: bool | None = None


class CDKCreateRequest(BaseModel):
    name: str = ""
    type: str = "quota"
    count: int = Field(default=1, ge=1, le=500)
    quota: int = Field(default=0, ge=0)
    package_id: str | None = None
    max_redemptions: int = Field(default=1, ge=1)
    per_user_limit: int = Field(default=1, ge=1)
    expires_at: str | None = None


class CDKUpdateRequest(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    expires_at: str | None = None


def _audit_admin_action(action: str, summary: str, **detail: object) -> None:
    safe_detail = {key: value for key, value in detail.items() if key not in {"token", "password", "code", "raw_key", "access_token"}}
    log_service.add(LOG_TYPE_ACCOUNT, summary, {"action": action, **safe_detail})


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/auth/users")
    async def list_user_keys(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"items": auth_service.list_keys(role="user")}

    @router.post("/api/auth/users")
    async def create_user_key(body: UserKeyCreateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        item, raw_key = auth_service.create_key(
            role="user",
            name=body.name,
            permissions=body.permissions,
            quota_limit=body.quota_limit,
            rate_limit_per_minute=body.rate_limit_per_minute,
            expires_at=body.expires_at,
            metadata=body.metadata,
        )
        return {"item": item, "key": raw_key, "items": auth_service.list_keys(role="user")}

    @router.post("/api/auth/users/{key_id}")
    async def update_user_key(
            key_id: str,
            body: UserKeyUpdateRequest,
            authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        updates = {
            key: value
            for key, value in {
                "name": body.name,
                "enabled": body.enabled,
                "permissions": body.permissions,
                "quota_limit": body.quota_limit,
                "rate_limit_per_minute": body.rate_limit_per_minute,
                "expires_at": body.expires_at,
                "metadata": body.metadata,
                "add_quota": body.add_quota,
            }.items()
            if value is not None
        }
        if body.reset_quota_used:
            updates["reset_quota_used"] = True
        if body.quota_unlimited:
            updates["quota_limit"] = None
        if body.rate_limit_unlimited:
            updates["rate_limit_per_minute"] = None
        if body.expires_never:
            updates["expires_at"] = None
        if not updates:
            raise HTTPException(status_code=400, detail={"error": "no updates provided"})
        item = auth_service.update_key(key_id, updates, role="user")
        if item is None:
            raise HTTPException(status_code=404, detail={"error": "user key not found"})
        return {"item": item, "items": auth_service.list_keys(role="user")}

    @router.delete("/api/auth/users/{key_id}")
    async def delete_user_key(key_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        if not auth_service.delete_key(key_id, role="user"):
            raise HTTPException(status_code=404, detail={"error": "user key not found"})
        return {"items": auth_service.list_keys(role="user")}

    @router.get("/api/admin/users")
    async def list_registered_users(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"items": auth_service.list_users()}

    @router.post("/api/admin/users")
    async def create_registered_user(body: AdminUserCreateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            item, token, key = auth_service.register_user(body.email, body.password, body.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        if body.quota_balance:
            item = auth_service.adjust_user_quota(str(item.get("id")), body.quota_balance, "admin-create", ref_type="admin_user_create") or item
        _audit_admin_action("user.create", "创建注册用户", user_id=item.get("id"), email=item.get("email"), initial_quota=body.quota_balance, quota_balance=item.get("quota_balance"))
        return {"item": item, "token": token, "key": key, "items": auth_service.list_users()}

    @router.post("/api/admin/users/{user_id}")
    async def update_registered_user(user_id: str, body: AdminUserUpdateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        updates = {key: value for key, value in body.model_dump(mode="python").items() if value is not None}
        if not updates:
            raise HTTPException(status_code=400, detail={"error": "no updates provided"})
        item = auth_service.update_user(user_id, updates)
        if item is None:
            raise HTTPException(status_code=404, detail={"error": "user not found"})
        _audit_admin_action("user.update", "更新注册用户", user_id=user_id, fields=sorted(updates.keys()))
        return {"item": item, "items": auth_service.list_users()}

    @router.post("/api/admin/users/{user_id}/password")
    async def reset_registered_user_password(user_id: str, body: AdminUserPasswordRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            item = auth_service.update_user(user_id, {"password": body.password})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        if item is None:
            raise HTTPException(status_code=404, detail={"error": "user not found"})
        _audit_admin_action("user.password_reset", "重置注册用户密码", user_id=user_id)
        return {"item": item}

    @router.post("/api/admin/users/{user_id}/quota")
    async def adjust_registered_user_quota(user_id: str, body: AdminUserQuotaRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        item = auth_service.adjust_user_quota(user_id, body.delta, body.reason or "admin-adjust", ref_type="admin_quota_adjust")
        if item is None:
            raise HTTPException(status_code=404, detail={"error": "user not found"})
        _audit_admin_action("user.quota_adjust", "调整注册用户额度", user_id=user_id, delta=body.delta, reason=body.reason)
        return {"item": item, "items": auth_service.list_users()}

    @router.get("/api/admin/packages")
    async def list_packages(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"items": redemption_service.list_packages()}

    @router.post("/api/admin/packages")
    async def create_package(body: PackageCreateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        item = redemption_service.create_package(
            name=body.name,
            description=body.description,
            quota=body.quota,
            valid_days=body.valid_days,
            price_cents=body.price_cents,
            currency=body.currency,
        )
        _audit_admin_action(
            "package.create",
            "创建套餐",
            package_id=item.get("id"),
            name=item.get("name"),
            quota=item.get("quota"),
            price_cents=item.get("price_cents"),
            currency=item.get("currency"),
            valid_days=item.get("valid_days"),
        )
        return {"item": item, "items": redemption_service.list_packages()}

    @router.post("/api/admin/packages/{package_id}")
    async def update_package(package_id: str, body: PackageUpdateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        updates = {key: value for key, value in body.model_dump(mode="python").items() if value is not None}
        if not updates:
            raise HTTPException(status_code=400, detail={"error": "no updates provided"})
        item = redemption_service.update_package(package_id, updates)
        if item is None:
            raise HTTPException(status_code=404, detail={"error": "package not found"})
        _audit_admin_action("package.update", "更新套餐", package_id=package_id, fields=sorted(updates.keys()))
        return {"item": item, "items": redemption_service.list_packages()}

    @router.get("/api/admin/cdks")
    async def list_cdks(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"items": redemption_service.list_cdks()}

    @router.post("/api/admin/cdks")
    async def create_cdks(body: CDKCreateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            result = redemption_service.create_cdks(**body.model_dump(mode="python"))
            _audit_admin_action("cdk.create", "创建 CDK", name=body.name, type=body.type, count=len(result.get("created", [])), package_id=body.package_id, quota=body.quota)
            return result
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    @router.post("/api/admin/cdks/{cdk_id}")
    async def update_cdk(cdk_id: str, body: CDKUpdateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        updates = {key: value for key, value in body.model_dump(mode="python").items() if value is not None}
        if not updates:
            raise HTTPException(status_code=400, detail={"error": "no updates provided"})
        item = redemption_service.update_cdk(cdk_id, updates)
        if item is None:
            raise HTTPException(status_code=404, detail={"error": "cdk not found"})
        _audit_admin_action("cdk.update", "更新 CDK", cdk_id=cdk_id, fields=sorted(updates.keys()))
        return {"item": item, "items": redemption_service.list_cdks()}

    @router.get("/api/admin/redemptions")
    async def list_redemptions(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"items": redemption_service.list_redemptions()}

    @router.get("/api/admin/quota-ledger")
    async def list_quota_ledger(
            user_id: str = "",
            limit: int = Query(default=200, ge=1, le=1000),
            authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        return {"items": auth_service.list_quota_ledger(user_id=user_id.strip() or None, limit=limit)}

    @router.get("/api/accounts")
    async def get_accounts(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"items": account_service.list_accounts()}

    @router.post("/api/accounts")
    async def create_accounts(body: AccountCreateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        tokens = [str(token or "").strip() for token in body.tokens if str(token or "").strip()]
        if not tokens:
            raise HTTPException(status_code=400, detail={"error": "tokens is required"})
        result = account_service.add_accounts(tokens)
        refresh_result = account_service.refresh_accounts(tokens)
        return {
            **result,
            "refreshed": refresh_result.get("refreshed", 0),
            "errors": refresh_result.get("errors", []),
            "items": refresh_result.get("items", result.get("items", [])),
        }

    @router.delete("/api/accounts")
    async def delete_accounts(body: AccountDeleteRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        tokens = [str(token or "").strip() for token in body.tokens if str(token or "").strip()]
        if not tokens:
            raise HTTPException(status_code=400, detail={"error": "tokens is required"})
        return account_service.delete_accounts(tokens)

    @router.post("/api/accounts/refresh")
    async def refresh_accounts(body: AccountRefreshRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        access_tokens = [str(token or "").strip() for token in body.access_tokens if str(token or "").strip()]
        if not access_tokens:
            access_tokens = account_service.list_tokens()
        if not access_tokens:
            raise HTTPException(status_code=400, detail={"error": "access_tokens is required"})
        return account_service.refresh_accounts(access_tokens)

    @router.post("/api/accounts/update")
    async def update_account(body: AccountUpdateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        access_token = str(body.access_token or "").strip()
        if not access_token:
            raise HTTPException(status_code=400, detail={"error": "access_token is required"})
        updates = {key: value for key, value in {
            "type": body.type,
            "status": body.status,
            "quota": body.quota,
            "disabled": body.disabled,
        }.items() if value is not None}
        if body.reset_consecutive_fail:
            updates["consecutive_fail"] = 0
        if not updates:
            raise HTTPException(status_code=400, detail={"error": "no updates provided"})
        account = account_service.update_account(access_token, updates)
        if account is None:
            raise HTTPException(status_code=404, detail={"error": "account not found"})
        return {"item": account, "items": account_service.list_accounts()}

    @router.get("/api/cpa/pools")
    async def list_cpa_pools(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"pools": sanitize_cpa_pools(cpa_config.list_pools())}

    @router.post("/api/cpa/pools")
    async def create_cpa_pool(body: CPAPoolCreateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        if not body.base_url.strip():
            raise HTTPException(status_code=400, detail={"error": "base_url is required"})
        if not body.secret_key.strip():
            raise HTTPException(status_code=400, detail={"error": "secret_key is required"})
        pool = cpa_config.add_pool(name=body.name, base_url=body.base_url, secret_key=body.secret_key)
        return {"pool": sanitize_cpa_pool(pool), "pools": sanitize_cpa_pools(cpa_config.list_pools())}

    @router.post("/api/cpa/pools/{pool_id}")
    async def update_cpa_pool(pool_id: str, body: CPAPoolUpdateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        pool = cpa_config.update_pool(pool_id, body.model_dump(exclude_none=True))
        if pool is None:
            raise HTTPException(status_code=404, detail={"error": "pool not found"})
        return {"pool": sanitize_cpa_pool(pool), "pools": sanitize_cpa_pools(cpa_config.list_pools())}

    @router.delete("/api/cpa/pools/{pool_id}")
    async def delete_cpa_pool(pool_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        if not cpa_config.delete_pool(pool_id):
            raise HTTPException(status_code=404, detail={"error": "pool not found"})
        return {"pools": sanitize_cpa_pools(cpa_config.list_pools())}

    @router.get("/api/cpa/pools/{pool_id}/files")
    async def cpa_pool_files(pool_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        pool = cpa_config.get_pool(pool_id)
        if pool is None:
            raise HTTPException(status_code=404, detail={"error": "pool not found"})
        return {"pool_id": pool_id, "files": await run_in_threadpool(list_remote_files, pool)}

    @router.post("/api/cpa/pools/{pool_id}/import")
    async def cpa_pool_import(pool_id: str, body: CPAImportRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        pool = cpa_config.get_pool(pool_id)
        if pool is None:
            raise HTTPException(status_code=404, detail={"error": "pool not found"})
        try:
            job = cpa_import_service.start_import(pool, body.names)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        return {"import_job": job}

    @router.get("/api/cpa/pools/{pool_id}/import")
    async def cpa_pool_import_progress(pool_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        pool = cpa_config.get_pool(pool_id)
        if pool is None:
            raise HTTPException(status_code=404, detail={"error": "pool not found"})
        return {"import_job": pool.get("import_job")}

    @router.get("/api/sub2api/servers")
    async def list_sub2api_servers(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"servers": sanitize_sub2api_servers(sub2api_config.list_servers())}

    @router.post("/api/sub2api/servers")
    async def create_sub2api_server(body: Sub2APIServerCreateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        if not body.base_url.strip():
            raise HTTPException(status_code=400, detail={"error": "base_url is required"})
        has_login = body.email.strip() and body.password.strip()
        has_api_key = bool(body.api_key.strip())
        if not has_login and not has_api_key:
            raise HTTPException(status_code=400, detail={"error": "email+password or api_key is required"})
        server = sub2api_config.add_server(
            name=body.name,
            base_url=body.base_url,
            email=body.email,
            password=body.password,
            api_key=body.api_key,
            group_id=body.group_id,
        )
        return {"server": sanitize_sub2api_server(server), "servers": sanitize_sub2api_servers(sub2api_config.list_servers())}

    @router.post("/api/sub2api/servers/{server_id}")
    async def update_sub2api_server(server_id: str, body: Sub2APIServerUpdateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        server = sub2api_config.update_server(server_id, body.model_dump(exclude_none=True))
        if server is None:
            raise HTTPException(status_code=404, detail={"error": "server not found"})
        return {"server": sanitize_sub2api_server(server), "servers": sanitize_sub2api_servers(sub2api_config.list_servers())}

    @router.delete("/api/sub2api/servers/{server_id}")
    async def delete_sub2api_server(server_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        if not sub2api_config.delete_server(server_id):
            raise HTTPException(status_code=404, detail={"error": "server not found"})
        return {"servers": sanitize_sub2api_servers(sub2api_config.list_servers())}

    @router.get("/api/sub2api/servers/{server_id}/groups")
    async def sub2api_server_groups(server_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        server = sub2api_config.get_server(server_id)
        if server is None:
            raise HTTPException(status_code=404, detail={"error": "server not found"})
        try:
            groups = await run_in_threadpool(sub2api_list_remote_groups, server)
        except Exception as exc:
            raise HTTPException(status_code=502, detail={"error": str(exc)}) from exc
        return {"server_id": server_id, "groups": groups}

    @router.get("/api/sub2api/servers/{server_id}/accounts")
    async def sub2api_server_accounts(server_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        server = sub2api_config.get_server(server_id)
        if server is None:
            raise HTTPException(status_code=404, detail={"error": "server not found"})
        try:
            accounts = await run_in_threadpool(sub2api_list_remote_accounts, server)
        except Exception as exc:
            raise HTTPException(status_code=502, detail={"error": str(exc)}) from exc
        return {"server_id": server_id, "accounts": accounts}

    @router.post("/api/sub2api/servers/{server_id}/import")
    async def sub2api_server_import(server_id: str, body: Sub2APIImportRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        server = sub2api_config.get_server(server_id)
        if server is None:
            raise HTTPException(status_code=404, detail={"error": "server not found"})
        try:
            job = sub2api_import_service.start_import(server, body.account_ids)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        return {"import_job": job}

    @router.get("/api/sub2api/servers/{server_id}/import")
    async def sub2api_server_import_progress(server_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        server = sub2api_config.get_server(server_id)
        if server is None:
            raise HTTPException(status_code=404, detail={"error": "server not found"})
        return {"import_job": server.get("import_job")}

    return router

