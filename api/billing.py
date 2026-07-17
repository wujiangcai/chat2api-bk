from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from api.support import require_admin, require_identity
from services.billing_service import BillingService, billing_service
from services.log_service import LOG_TYPE_ACCOUNT, log_service
from services.payment_checkout_service import PaymentCheckoutError, PaymentCheckoutService
from services.payment_webhook_service import (
    PaymentWebhookPayloadError,
    PaymentWebhookService,
    PaymentWebhookSignatureError,
    payment_webhook_service,
)
from services.receipt_service import ReceiptService


class OrderCreateRequest(BaseModel):
    package_id: str = ""
    quantity: int = Field(default=1, ge=1, le=100)
    metadata: dict[str, object] | None = None


class OrderCancelRequest(BaseModel):
    reason: str = ""


class CheckoutCreateRequest(BaseModel):
    provider: str = ""
    success_url: str = ""
    cancel_url: str = ""
    metadata: dict[str, object] | None = None


class OrderRefundRequest(BaseModel):
    reason: str = ""
    metadata: dict[str, object] | None = None


class MarkPaidRequest(BaseModel):
    provider: str = "manual"
    provider_payment_id: str = ""
    amount_cents: int | None = Field(default=None, ge=0)
    currency: str | None = None
    idempotency_key: str = ""
    metadata: dict[str, object] | None = None
    auto_fulfill: bool = True


class MockPaymentNotifyRequest(BaseModel):
    order_id: str = ""
    provider: str = "mock"
    provider_payment_id: str = ""
    amount_cents: int | None = Field(default=None, ge=0)
    currency: str | None = None
    idempotency_key: str = ""
    metadata: dict[str, object] | None = None


def _registered_user_id(identity: dict[str, object]) -> str:
    user_id = str(identity.get("user_id") or "").strip()
    if not user_id:
        raise HTTPException(status_code=403, detail={"error": "registered user is required"})
    return user_id


def _audit_billing_action(action: str, summary: str, **detail: object) -> None:
    safe_detail = {key: value for key, value in detail.items() if key not in {"token", "password", "raw_key"}}
    log_service.add(LOG_TYPE_ACCOUNT, summary, {"action": action, **safe_detail})


def create_router(
    service: BillingService | None = None,
    webhook_service: PaymentWebhookService | None = None,
    checkout_service: PaymentCheckoutService | None = None,
) -> APIRouter:
    router = APIRouter()
    billing = service or billing_service
    webhooks = webhook_service or (payment_webhook_service if service is None else PaymentWebhookService(billing))
    checkouts = checkout_service or PaymentCheckoutService(billing)
    receipts = ReceiptService(billing)

    @router.get("/api/packages")
    async def list_public_packages(authorization: str | None = Header(default=None)):
        require_identity(authorization)
        items = [item for item in billing.redemption_service.list_packages() if bool(item.get("enabled", True))]
        return {"items": items}

    @router.post("/api/orders")
    async def create_order(body: OrderCreateRequest, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        user_id = _registered_user_id(identity)
        try:
            order = billing.create_order(
                user_id=user_id,
                email=str(identity.get("email") or ""),
                package_id=body.package_id,
                quantity=body.quantity,
                metadata=body.metadata,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        return {"order": order}

    @router.post("/api/orders/{order_id}/checkout")
    async def create_order_checkout(order_id: str, body: CheckoutCreateRequest | None = None, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        _registered_user_id(identity)
        try:
            return checkouts.create_checkout(
                order_id,
                identity,
                provider=(body.provider if body else ""),
                success_url=(body.success_url if body else ""),
                cancel_url=(body.cancel_url if body else ""),
                metadata=(body.metadata if body else None),
            )
        except PaymentCheckoutError as exc:
            detail = str(exc)
            status_code = 404 if "not found" in detail else 400
            raise HTTPException(status_code=status_code, detail={"error": detail}) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    @router.get("/api/orders")
    async def list_my_orders(
            status: str = "",
            limit: int = Query(default=100, ge=1, le=500),
            authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        _registered_user_id(identity)
        return {"items": billing.list_orders(identity, status=status or None, limit=limit)}

    @router.get("/api/orders/{order_id}")
    async def get_my_order(order_id: str, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        _registered_user_id(identity)
        order = billing.get_order(order_id, identity)
        if order is None:
            raise HTTPException(status_code=404, detail={"error": "order not found"})
        return {"order": order}

    @router.get("/api/orders/{order_id}/receipt")
    async def get_my_order_receipt(
            order_id: str,
            format: str = Query(default="json"),
            authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        _registered_user_id(identity)
        try:
            receipt = receipts.build_order_receipt(order_id, identity)
        except ValueError as exc:
            detail = str(exc)
            status_code = 404 if "not found" in detail else 400
            raise HTTPException(status_code=status_code, detail={"error": detail}) from exc
        if format.strip().lower() == "html":
            return HTMLResponse(receipts.render_html(receipt))
        return {"receipt": receipt}

    @router.post("/api/orders/{order_id}/cancel")
    async def cancel_my_order(order_id: str, body: OrderCancelRequest | None = None, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        _registered_user_id(identity)
        try:
            order = billing.cancel_order(order_id, identity, reason=(body.reason if body else ""))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        if order is None:
            raise HTTPException(status_code=404, detail={"error": "order not found"})
        return {"order": order}

    @router.get("/api/admin/orders")
    async def list_admin_orders(
            status: str = "",
            limit: int = Query(default=200, ge=1, le=1000),
            authorization: str | None = Header(default=None),
    ):
        identity = require_admin(authorization)
        return {"items": billing.list_orders(identity, status=status or None, limit=limit)}

    @router.get("/api/admin/payments")
    async def list_admin_payments(
            provider: str = "",
            status: str = "",
            limit: int = Query(default=200, ge=1, le=1000),
            authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        return {"items": billing.list_payments(provider=provider or None, status=status or None, limit=limit)}

    @router.get("/api/admin/orders/{order_id}/receipt")
    async def get_admin_order_receipt(
            order_id: str,
            format: str = Query(default="json"),
            authorization: str | None = Header(default=None),
    ):
        actor = require_admin(authorization)
        try:
            receipt = receipts.build_order_receipt(order_id, actor)
        except ValueError as exc:
            detail = str(exc)
            status_code = 404 if "not found" in detail else 400
            raise HTTPException(status_code=status_code, detail={"error": detail}) from exc
        if format.strip().lower() == "html":
            return HTMLResponse(receipts.render_html(receipt))
        return {"receipt": receipt}

    @router.post("/api/admin/orders/{order_id}/mark-paid")
    async def mark_admin_order_paid(order_id: str, body: MarkPaidRequest, authorization: str | None = Header(default=None)):
        actor = require_admin(authorization)
        try:
            result = billing.mark_paid(
                order_id,
                provider=body.provider,
                provider_payment_id=body.provider_payment_id,
                amount_cents=body.amount_cents,
                currency=body.currency,
                idempotency_key=body.idempotency_key,
                actor=actor,
                metadata=body.metadata,
                auto_fulfill=body.auto_fulfill,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        _audit_billing_action(
            "order.mark_paid",
            "订单确认支付",
            order_id=order_id,
            provider=body.provider,
            provider_payment_id=body.provider_payment_id,
            amount_cents=body.amount_cents,
            auto_fulfill=body.auto_fulfill,
        )
        return result

    @router.post("/api/admin/orders/{order_id}/fulfill")
    async def fulfill_admin_order(order_id: str, authorization: str | None = Header(default=None)):
        actor = require_admin(authorization)
        try:
            result = billing.fulfill_order(order_id, actor=actor)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        _audit_billing_action("order.fulfill", "订单履约发放额度", order_id=order_id)
        return result

    @router.post("/api/admin/orders/{order_id}/refund")
    async def refund_admin_order(order_id: str, body: OrderRefundRequest | None = None, authorization: str | None = Header(default=None)):
        actor = require_admin(authorization)
        try:
            result = billing.refund_order(
                order_id,
                actor=actor,
                reason=(body.reason if body else ""),
                metadata=(body.metadata if body else None),
            )
        except ValueError as exc:
            detail = str(exc)
            status_code = 404 if "not found" in detail else 400
            raise HTTPException(status_code=status_code, detail={"error": detail}) from exc
        _audit_billing_action(
            "order.refund",
            "订单退款并扣回额度",
            order_id=order_id,
            reason=(body.reason if body else ""),
            quota_deducted=result.get("quota_deducted"),
            idempotent=result.get("idempotent"),
        )
        return result

    @router.post("/api/admin/orders/{order_id}/cancel")
    async def cancel_admin_order(order_id: str, body: OrderCancelRequest | None = None, authorization: str | None = Header(default=None)):
        actor = require_admin(authorization)
        try:
            order = billing.cancel_order(order_id, actor, reason=(body.reason if body else "admin"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        if order is None:
            raise HTTPException(status_code=404, detail={"error": "order not found"})
        _audit_billing_action("order.cancel", "取消订单", order_id=order_id, reason=(body.reason if body else "admin"))
        return {"order": order}

    @router.post("/api/admin/orders/{order_id}/checkout")
    async def create_admin_order_checkout(order_id: str, body: CheckoutCreateRequest | None = None, authorization: str | None = Header(default=None)):
        actor = require_admin(authorization)
        try:
            result = checkouts.create_checkout(
                order_id,
                actor,
                provider=(body.provider if body else ""),
                success_url=(body.success_url if body else ""),
                cancel_url=(body.cancel_url if body else ""),
                metadata=(body.metadata if body else None),
            )
        except PaymentCheckoutError as exc:
            detail = str(exc)
            status_code = 404 if "not found" in detail else 400
            raise HTTPException(status_code=status_code, detail={"error": detail}) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        _audit_billing_action(
            "order.checkout",
            "创建订单支付入口",
            order_id=order_id,
            provider=result.get("checkout", {}).get("provider") if isinstance(result.get("checkout"), dict) else None,
            checkout_id=result.get("checkout", {}).get("id") if isinstance(result.get("checkout"), dict) else None,
        )
        return result

    @router.post("/api/payments/mock/notify")
    async def mock_payment_notify(body: MockPaymentNotifyRequest, authorization: str | None = Header(default=None)):
        actor = require_admin(authorization)
        if not body.order_id.strip():
            raise HTTPException(status_code=400, detail={"error": "order_id is required"})
        try:
            result = billing.mark_paid(
                body.order_id,
                provider=body.provider or "mock",
                provider_payment_id=body.provider_payment_id,
                amount_cents=body.amount_cents,
                currency=body.currency,
                idempotency_key=body.idempotency_key,
                actor=actor,
                metadata=body.metadata,
                auto_fulfill=True,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        _audit_billing_action(
            "payment.mock_notify",
            "模拟支付回调",
            order_id=body.order_id,
            provider=body.provider,
            provider_payment_id=body.provider_payment_id,
        )
        return result

    @router.post("/api/payments/webhook/{provider}")
    async def signed_payment_webhook(provider: str, request: Request):
        body = await request.body()
        try:
            result = webhooks.handle(provider, body, request.headers)
        except PaymentWebhookSignatureError as exc:
            raise HTTPException(status_code=401, detail={"error": str(exc)}) from exc
        except PaymentWebhookPayloadError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        except ValueError as exc:
            detail = str(exc)
            status_code = 409 if "conflict" in detail.lower() else 400
            raise HTTPException(status_code=status_code, detail={"error": detail}) from exc
        _audit_billing_action(
            "payment.webhook",
            "signed payment webhook",
            provider=provider,
            event_type=result.get("event_type"),
            status=result.get("status"),
            order_id=result.get("order_id"),
            event_id=result.get("event_id"),
            ignored=result.get("ignored"),
            idempotent=result.get("idempotent"),
        )
        return result

    return router
