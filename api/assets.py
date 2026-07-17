from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query, Request

from api.support import require_admin, require_identity, resolve_image_base_url
from services.image_asset_service import image_asset_service


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/assets")
    async def list_my_assets(
            request: Request,
            start_date: str = "",
            end_date: str = "",
            limit: int = Query(default=100, ge=1, le=1000),
            authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        return {
            "items": image_asset_service.list_assets(
                identity,
                base_url=resolve_image_base_url(request),
                limit=limit,
                start_date=start_date.strip(),
                end_date=end_date.strip(),
            )
        }

    @router.get("/api/assets/{asset_id}")
    async def get_my_asset(asset_id: str, request: Request, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        asset = image_asset_service.get_asset(asset_id, identity, base_url=resolve_image_base_url(request))
        if asset is None or asset.get("status") == "deleted":
            raise HTTPException(status_code=404, detail={"error": "asset not found"})
        return {"asset": asset}

    @router.delete("/api/assets/{asset_id}")
    async def delete_my_asset(asset_id: str, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        asset = image_asset_service.delete_asset(asset_id, identity)
        if asset is None:
            raise HTTPException(status_code=404, detail={"error": "asset not found"})
        return {"asset": asset}

    @router.get("/api/admin/assets")
    async def list_admin_assets(
            request: Request,
            start_date: str = "",
            end_date: str = "",
            limit: int = Query(default=200, ge=1, le=1000),
            include_deleted: bool = False,
            authorization: str | None = Header(default=None),
    ):
        identity = require_admin(authorization)
        return {
            "items": image_asset_service.list_assets(
                identity,
                base_url=resolve_image_base_url(request),
                limit=limit,
                start_date=start_date.strip(),
                end_date=end_date.strip(),
                include_deleted=include_deleted,
            )
        }

    @router.delete("/api/admin/assets/{asset_id}")
    async def delete_admin_asset(asset_id: str, authorization: str | None = Header(default=None)):
        identity = require_admin(authorization)
        asset = image_asset_service.delete_asset(asset_id, identity)
        if asset is None:
            raise HTTPException(status_code=404, detail={"error": "asset not found"})
        return {"asset": asset}

    return router
