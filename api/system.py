from __future__ import annotations

import os
import time

from fastapi import APIRouter, Cookie, Header, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from api.support import require_admin, require_identity, resolve_image_base_url
from services.audit_service import audit_service
from services.auth_service import auth_service
from services.billing_service import billing_service
from services.config import config
from services.email_service import email_service
from services.image_service import list_images
from services.log_service import log_service
from services.launch_evidence_service import launch_evidence_service
from services.monitoring_service import monitoring_service
from services.payment_checkout_service import PaymentCheckoutError, PaymentCheckoutService
from services.payment_webhook_service import PaymentWebhookError, payment_webhook_service
from services.production_readiness import production_readiness_service
from services.proxy_service import test_proxy
from services.rate_limit_service import create_rate_limiter_from_env
from services.redemption_service import redemption_service
from services.reporting_service import reporting_service
from scripts.payment_webhook_sandbox import build_payload as build_webhook_payload
from scripts.payment_webhook_sandbox import canonical_body as canonical_webhook_body
from scripts.payment_webhook_sandbox import normalize_provider as normalize_webhook_provider
from scripts.payment_webhook_sandbox import sign_headers as sign_webhook_headers


class SettingsUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="allow")


class ProxyTestRequest(BaseModel):
    url: str = ""


class AuthLoginRequest(BaseModel):
    email: str = ""
    password: str = ""


class AuthRegisterRequest(BaseModel):
    email: str = ""
    password: str = Field(default="", min_length=8)
    name: str = ""


class AuthEmailVerificationRequest(BaseModel):
    email: str = ""


class AuthTokenConfirmRequest(BaseModel):
    token: str = ""


class AuthPasswordResetRequest(BaseModel):
    email: str = ""


class AuthPasswordResetConfirmRequest(BaseModel):
    token: str = ""
    password: str = Field(default="", min_length=8)


class RedeemRequest(BaseModel):
    code: str = ""


class LaunchEvidenceCreateRequest(BaseModel):
    name: str = ""
    source: str = "manual-upload"
    report: dict[str, object] = Field(default_factory=dict)


class PaymentWebhookReplayRequest(BaseModel):
    provider: str = "stripe"
    secret: str = ""
    package_id: str = ""
    amount_cents: int = Field(default=1990, ge=0)
    currency: str = "CNY"
    quota: int = Field(default=1, ge=1, le=100000)
    run_refund: bool = True
    archive: bool = True
    evidence_name: str = ""
    email: str = ""


class CheckoutWebhookReplayRequest(BaseModel):
    checkout_provider: str = ""
    webhook_provider: str = "stripe"
    secret: str = ""
    amount_cents: int = Field(default=1990, ge=1)
    currency: str = "CNY"
    quota: int = Field(default=1, ge=1, le=100000)
    run_refund: bool = True
    archive: bool = True
    evidence_name: str = ""
    email: str = ""


LOGIN_RATE_LIMIT_PER_MINUTE = int(os.getenv("LOGIN_RATE_LIMIT_PER_MINUTE", "8"))
REGISTER_RATE_LIMIT_PER_MINUTE = int(os.getenv("REGISTER_RATE_LIMIT_PER_MINUTE", "4"))
REDEEM_RATE_LIMIT_PER_MINUTE = int(os.getenv("REDEEM_RATE_LIMIT_PER_MINUTE", "12"))
KEY_LOGIN_RATE_LIMIT_PER_MINUTE = int(os.getenv("KEY_LOGIN_RATE_LIMIT_PER_MINUTE", "12"))
REGISTRATION_ENABLED = os.getenv("REGISTRATION_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}
EMAIL_VERIFICATION_REQUIRED = os.getenv("EMAIL_VERIFICATION_REQUIRED", "false").strip().lower() in {"1", "true", "yes", "on"}
AUTH_SESSION_COOKIE_NAME = os.getenv("AUTH_SESSION_COOKIE_NAME", "chatgpt2api_session").strip() or "chatgpt2api_session"
_login_attempts: dict[str, list[float]] = {}
_public_rate_limiter = create_rate_limiter_from_env(namespace="public-actions", memory_bucket=_login_attempts)


def _rate_limit_key(request: Request | None, namespace: str, subject: str = "") -> str:
    host = request.client.host if request and request.client else "unknown"
    return f"{namespace}:{host}:{str(subject or 'unknown').strip().lower()}"


def _check_action_rate_limit(bucket: dict[str, list[float]], key: str, limit: int, label: str) -> None:
    if limit <= 0:
        return
    limiter = _public_rate_limiter
    result = limiter.allow(key, limit, window_seconds=60, cost=1)
    if not result.allowed:
        raise HTTPException(
            status_code=429,
            detail={"error": f"too many {label} attempts: max {limit} per minute"},
            headers={"Retry-After": str(result.retry_after_seconds or 60)},
        )


def _check_login_rate_limit(email: str, request: Request | None = None) -> None:
    _check_action_rate_limit(_login_attempts, _rate_limit_key(request, "login", email), LOGIN_RATE_LIMIT_PER_MINUTE, "login")


def _check_key_login_rate_limit(authorization: str | None, request: Request | None = None) -> None:
    token_prefix = str(authorization or "").strip()[:18]
    _check_action_rate_limit(_login_attempts, _rate_limit_key(request, "key-login", token_prefix), KEY_LOGIN_RATE_LIMIT_PER_MINUTE, "key login")


def _check_register_rate_limit(email: str, request: Request | None = None) -> None:
    ip_key = _rate_limit_key(request, "register-ip", "")
    _check_action_rate_limit(_login_attempts, ip_key, REGISTER_RATE_LIMIT_PER_MINUTE, "registration")
    _check_action_rate_limit(_login_attempts, _rate_limit_key(request, "register", email), REGISTER_RATE_LIMIT_PER_MINUTE, "registration")


def _check_redeem_rate_limit(identity: dict[str, object] | None, request: Request | None = None) -> None:
    subject = str((identity or {}).get("user_id") or (identity or {}).get("id") or "anonymous")
    ip_key = _rate_limit_key(request, "redeem-ip", "")
    _check_action_rate_limit(_login_attempts, ip_key, REDEEM_RATE_LIMIT_PER_MINUTE, "redeem")
    _check_action_rate_limit(_login_attempts, _rate_limit_key(request, "redeem", subject), REDEEM_RATE_LIMIT_PER_MINUTE, "redeem")


def _clear_login_attempts(email: str, request: Request | None = None) -> None:
    _public_rate_limiter.clear(_rate_limit_key(request, "login", email))


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _session_cookie_ttl_seconds() -> int:
    try:
        return max(60, int(os.getenv("AUTH_SESSION_COOKIE_TTL_SECONDS", "2592000")))
    except (TypeError, ValueError):
        return 2592000


def _auth_cookie_enabled() -> bool:
    return _env_bool("AUTH_SESSION_COOKIE_ENABLED", True)


def _auth_cookie_secure() -> bool:
    raw = os.getenv("AUTH_SESSION_COOKIE_SECURE")
    if raw is not None:
        return _env_bool("AUTH_SESSION_COOKIE_SECURE", False)
    return bool(config.is_production or config.base_url.startswith("https://"))


def _auth_cookie_samesite() -> str:
    value = str(os.getenv("AUTH_SESSION_COOKIE_SAMESITE", "lax") or "lax").strip().lower()
    return value if value in {"lax", "strict", "none"} else "lax"


def _should_return_auth_tokens() -> bool:
    raw = os.getenv("AUTH_RESPONSE_INCLUDE_TOKEN")
    if raw is not None:
        return _env_bool("AUTH_RESPONSE_INCLUDE_TOKEN", True)
    return not config.is_production


def _should_return_action_tokens() -> bool:
    raw = os.getenv("AUTH_RETURN_ACTION_TOKENS")
    if raw is not None:
        return _env_bool("AUTH_RETURN_ACTION_TOKENS", False)
    return not config.is_production


def _email_delivery_required() -> bool:
    raw = os.getenv("EMAIL_DELIVERY_REQUIRED")
    if raw is None:
        return False
    return _env_bool("EMAIL_DELIVERY_REQUIRED", False)


def _set_session_cookie(response: Response, identity: dict[str, object]) -> bool:
    if not _auth_cookie_enabled():
        return False
    ttl_seconds = _session_cookie_ttl_seconds()
    _, session_token = auth_service.create_session(identity, ttl_seconds=ttl_seconds)
    response.set_cookie(
        AUTH_SESSION_COOKIE_NAME,
        session_token,
        max_age=ttl_seconds,
        httponly=True,
        secure=_auth_cookie_secure() or _auth_cookie_samesite() == "none",
        samesite=_auth_cookie_samesite(),  # type: ignore[arg-type]
        path="/",
    )
    return True


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(AUTH_SESSION_COOKIE_NAME, path="/")


def _authorization_with_cookie(authorization: str | None, session_cookie: str | None) -> str | None:
    if authorization:
        return authorization
    token = str(session_cookie or "").strip()
    return f"Bearer {token}" if token else None


def _session_response(app_version: str, identity: dict[str, object], token: str | None = None, *, session_cookie: bool = False) -> dict[str, object]:
    response = {
        "ok": True,
        "version": app_version,
        "role": identity.get("role"),
        "subject_id": identity.get("user_id") or identity.get("id"),
        "key_id": identity.get("key_id") or identity.get("id"),
        "email": identity.get("email"),
        "name": identity.get("name"),
        "quota_balance": identity.get("quota_balance"),
        "package_name": identity.get("package_name"),
        "email_verified": bool(identity.get("email_verified") or identity.get("email_verified_at")),
        "email_verified_at": identity.get("email_verified_at"),
        "session_cookie": session_cookie,
    }
    if token and _should_return_auth_tokens():
        response["token"] = token
    return response


def _public_identity(identity: dict[str, object]) -> dict[str, object]:
    return {
        "id": identity.get("id"),
        "key_id": identity.get("key_id") or identity.get("id"),
        "user_id": identity.get("user_id"),
        "email": identity.get("email"),
        "name": identity.get("name"),
        "role": identity.get("role"),
        "permissions": identity.get("permissions") or [],
        "quota_limit": identity.get("quota_limit"),
        "quota_used": identity.get("quota_used"),
        "quota_remaining": identity.get("quota_remaining"),
        "quota_balance": identity.get("quota_balance"),
        "package_id": identity.get("package_id"),
        "package_name": identity.get("package_name"),
        "package_expires_at": identity.get("package_expires_at"),
        "email_verified": bool(identity.get("email_verified") or identity.get("email_verified_at")),
        "email_verified_at": identity.get("email_verified_at"),
        "rate_limit_per_minute": identity.get("rate_limit_per_minute"),
        "expires_at": identity.get("expires_at"),
    }


def _verification_token_payload(
        item: dict[str, object] | None,
        raw_token: str | None,
        *,
        email_delivery: dict[str, object] | None = None,
) -> dict[str, object]:
    payload = {"ok": True, "sent": item is not None}
    if item:
        payload["expires_at"] = item.get("expires_at")
        payload["email"] = item.get("email")
    if raw_token and _should_return_action_tokens():
        payload["token"] = raw_token
    if email_delivery is not None:
        payload["email_sent"] = bool(email_delivery.get("sent"))
        payload["email_provider"] = email_delivery.get("provider")
        payload["email_delivery"] = email_delivery
    return payload


async def _send_action_email(
        kind: str,
        item: dict[str, object] | None,
        raw_token: str | None,
) -> dict[str, object] | None:
    if item is None or not raw_token:
        return None
    email = str(item.get("email") or "").strip()
    if not email:
        return None
    try:
        if kind == "password_reset":
            result = await run_in_threadpool(
                email_service.send_password_reset,
                to=email,
                token=raw_token,
                expires_at=str(item.get("expires_at") or "") or None,
            )
        else:
            result = await run_in_threadpool(
                email_service.send_email_verification,
                to=email,
                token=raw_token,
                expires_at=str(item.get("expires_at") or "") or None,
            )
        return result.as_public_dict()
    except Exception as exc:
        if _email_delivery_required():
            message = "email delivery failed" if config.is_production else f"email delivery failed: {exc}"
            raise HTTPException(status_code=503, detail={"error": message}) from exc
        return {
            "sent": False,
            "provider": email_service.provider,
            "message": "email delivery failed" if config.is_production else str(exc),
        }


def _safe_check(check_id: str, passed: bool, message: str, detail: dict[str, object] | None = None) -> dict[str, object]:
    item: dict[str, object] = {
        "id": check_id,
        "status": "passed" if passed else "failed",
        "message": message,
    }
    if detail:
        item["detail"] = detail
    return item


def _provider_secret(provider: str, explicit_secret: str = "") -> str:
    explicit = str(explicit_secret or "").strip()
    if explicit:
        return explicit
    safe_provider = "".join(ch if ch.isalnum() else "_" for ch in provider.upper()).strip("_")
    if safe_provider:
        provider_secret = os.getenv(f"PAYMENT_WEBHOOK_SECRET_{safe_provider}", "")
        if provider_secret:
            return provider_secret
    return os.getenv("PAYMENT_WEBHOOK_SECRET", "")


def _payment_webhook_replay_report(
        *,
        actor: dict[str, object],
        provider: str,
        secret: str,
        package_id: str,
        amount_cents: int,
        currency: str,
        quota: int,
        run_refund: bool,
        email: str = "",
) -> dict[str, object]:
    normalized_provider = normalize_webhook_provider(provider or "stripe")
    normalized_secret = _provider_secret(normalized_provider, secret)
    if not normalized_secret:
        raise ValueError("payment webhook secret is required")

    now = int(time.time())
    package_created = False
    validation_package = None
    if package_id.strip():
        validation_package = redemption_service.get_package(package_id.strip(), include_disabled=False)
        if validation_package is None:
            raise ValueError("package is invalid")
    else:
        validation_package = redemption_service.create_package(
            name=f"Webhook verification {now}",
            description="Temporary package for paid/refund webhook launch evidence replay.",
            quota=quota,
            price_cents=amount_cents,
            currency=currency,
        )
        package_created = True

    validation_email = str(email or "").strip().lower() or f"webhook-verifier+{now}@example.invalid"
    user, _, _ = auth_service.register_user(validation_email, f"WebhookVerifier{now}A!", "Webhook Verifier")
    order = billing_service.create_order(
        user_id=str(user.get("id") or ""),
        email=str(user.get("email") or validation_email),
        package_id=str(validation_package.get("id") or ""),
        metadata={
            "purpose": "payment_webhook_replay",
            "provider": normalized_provider,
            "created_by": actor.get("user_id") or actor.get("id") or actor.get("key_id"),
        },
    )

    checks: list[dict[str, object]] = [
        _safe_check(
            "payment_webhook.admin_replay.order_created",
            True,
            "Disposable validation order was created",
            {
                "order_id": order.get("id"),
                "user_id": user.get("id"),
                "package_id": validation_package.get("id"),
                "package_created": package_created,
            },
        )
    ]
    paid_result: dict[str, object] | None = None
    refund_result: dict[str, object] | None = None
    paid_payment_id = f"admin_verify_paid_{order.get('id')}_{now}"

    try:
        paid_payload = build_webhook_payload(
            provider=normalized_provider,
            action="paid",
            order_id=str(order.get("id") or ""),
            amount_cents=amount_cents,
            currency=currency,
            provider_payment_id=paid_payment_id,
            event_id=f"evt_admin_verify_paid_{now}",
        )
        paid_body = canonical_webhook_body(paid_payload)
        paid_headers = sign_webhook_headers(provider=normalized_provider, secret=normalized_secret, body=paid_body)
        paid_result = payment_webhook_service.handle(normalized_provider, paid_body, paid_headers, secret_override=normalized_secret)
        paid_order = paid_result.get("order") if isinstance(paid_result.get("order"), dict) else {}
        paid_payment = paid_result.get("payment") if isinstance(paid_result.get("payment"), dict) else {}
        paid_ok = (
            paid_result.get("ok") is True
            and paid_result.get("ignored") is False
            and paid_result.get("action") == "mark_paid"
            and paid_order.get("status") in {"paid", "fulfilled"}
            and paid_payment.get("status") == "succeeded"
        )
        checks.append(
            _safe_check(
                "payment_webhook.admin_replay.paid",
                paid_ok,
                "Signed paid webhook replay fulfilled the disposable order" if paid_ok else "Signed paid webhook replay failed",
                {
                    "order_status": paid_order.get("status"),
                    "payment_status": paid_payment.get("status"),
                    "action": paid_result.get("action"),
                },
            )
        )
    except PaymentWebhookError as exc:
        checks.append(_safe_check("payment_webhook.admin_replay.paid", False, str(exc)))

    if run_refund:
        paid_ok = any(item.get("id") == "payment_webhook.admin_replay.paid" and item.get("status") == "passed" for item in checks)
        if paid_ok:
            try:
                paid_payment = paid_result.get("payment") if isinstance((paid_result or {}).get("payment"), dict) else {}
                refund_payload = build_webhook_payload(
                    provider=normalized_provider,
                    action="refund",
                    order_id=str(order.get("id") or ""),
                    amount_cents=amount_cents,
                    currency=currency,
                    provider_payment_id=str(paid_payment.get("provider_payment_id") or paid_payment_id),
                    event_id=f"evt_admin_verify_refund_{now}",
                )
                refund_body = canonical_webhook_body(refund_payload)
                refund_headers = sign_webhook_headers(provider=normalized_provider, secret=normalized_secret, body=refund_body)
                refund_result = payment_webhook_service.handle(normalized_provider, refund_body, refund_headers, secret_override=normalized_secret)
                refund_order = refund_result.get("order") if isinstance(refund_result.get("order"), dict) else {}
                refund_payment = refund_result.get("payment") if isinstance(refund_result.get("payment"), dict) else {}
                refund_ok = (
                    refund_result.get("ok") is True
                    and refund_result.get("ignored") is False
                    and refund_result.get("action") == "refund"
                    and refund_order.get("status") == "refunded"
                    and refund_payment.get("status") == "refunded"
                )
                checks.append(
                    _safe_check(
                        "payment_webhook.admin_replay.refund",
                        refund_ok,
                        "Signed refund webhook replay refunded the disposable order" if refund_ok else "Signed refund webhook replay failed",
                        {
                            "order_status": refund_order.get("status"),
                            "payment_status": refund_payment.get("status"),
                            "action": refund_result.get("action"),
                            "quota_deducted": refund_result.get("quota_deducted"),
                        },
                    )
                )
            except PaymentWebhookError as exc:
                checks.append(_safe_check("payment_webhook.admin_replay.refund", False, str(exc)))
        else:
            checks.append(_safe_check("payment_webhook.admin_replay.refund", False, "Refund replay skipped because paid replay failed"))

    # Keep verification artifacts from appearing as normal sellable/user-active records.
    if package_created:
        redemption_service.update_package(str(validation_package.get("id") or ""), {"enabled": False})
    auth_service.update_user(str(user.get("id") or ""), {"enabled": False})

    failed = [item for item in checks if item.get("status") == "failed"]
    passed = [item for item in checks if item.get("status") == "passed"]
    return {
        "status": "failed" if failed else "passed",
        "ready": not failed,
        "source": "admin-payment-webhook-replay",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "provider": normalized_provider,
        "order_id": order.get("id"),
        "user_id": user.get("id"),
        "package_id": validation_package.get("id"),
        "summary": {
            "total": len(checks),
            "passed": len(passed),
            "warning": 0,
            "failed": len(failed),
        },
        "evidence": {
            "payment_webhook_replay_requested": True,
            "payment_webhook_paid_replay": any(item.get("id") == "payment_webhook.admin_replay.paid" and item.get("status") == "passed" for item in checks),
            "payment_webhook_refund_replay": any(item.get("id") == "payment_webhook.admin_replay.refund" and item.get("status") == "passed" for item in checks),
            "disposable_order_id": order.get("id"),
            "disposable_user_disabled": True,
            "temporary_package_disabled": package_created,
        },
        "checks": checks,
    }


def _checkout_webhook_replay_report(
        *,
        actor: dict[str, object],
        checkout_provider: str,
        webhook_provider: str,
        secret: str,
        amount_cents: int,
        currency: str,
        quota: int,
        run_refund: bool,
        email: str = "",
) -> dict[str, object]:
    normalized_webhook_provider = normalize_webhook_provider(webhook_provider or "stripe")
    normalized_secret = _provider_secret(normalized_webhook_provider, secret)
    if not normalized_secret:
        raise ValueError("payment webhook secret is required")

    now = int(time.time())
    unique_suffix = f"{now}-{time.time_ns() % 1_000_000}"
    requested_checkout_provider = str(checkout_provider or "").strip()
    checkout_provider_used = requested_checkout_provider or "default"
    validation_package: dict[str, object] | None = None
    user: dict[str, object] | None = None
    order: dict[str, object] | None = None
    checkout_result: dict[str, object] | None = None
    checkout: dict[str, object] = {}
    paid_result: dict[str, object] | None = None
    checks: list[dict[str, object]] = []
    package_disabled = False
    user_disabled = False

    def check_passed(check_id: str) -> bool:
        return any(item.get("id") == check_id and item.get("status") == "passed" for item in checks)

    try:
        validation_package = redemption_service.create_package(
            name=f"Checkout verification {unique_suffix}",
            description="Temporary package for checkout + paid/refund webhook launch evidence replay.",
            quota=quota,
            price_cents=amount_cents,
            currency=currency,
        )
        validation_email = str(email or "").strip().lower() or f"checkout-verifier+{unique_suffix}@example.invalid"
        user, _, _ = auth_service.register_user(validation_email, f"CheckoutVerifier{now}A!", "Checkout Verifier")
        order = billing_service.create_order(
            user_id=str(user.get("id") or ""),
            email=str(user.get("email") or validation_email),
            package_id=str(validation_package.get("id") or ""),
            metadata={
                "purpose": "checkout_webhook_replay",
                "checkout_provider": requested_checkout_provider or "default",
                "webhook_provider": normalized_webhook_provider,
                "created_by": actor.get("user_id") or actor.get("id") or actor.get("key_id"),
            },
        )
        checks.append(
            _safe_check(
                "payment_checkout.admin_replay.fixtures",
                True,
                "Disposable checkout validation package, user and order were created",
                {
                    "order_id": order.get("id"),
                    "user_id": user.get("id"),
                    "package_id": validation_package.get("id"),
                    "amount_cents": amount_cents,
                    "currency": currency,
                    "quota": quota,
                },
            )
        )
    except Exception as exc:
        checks.append(_safe_check("payment_checkout.admin_replay.fixtures", False, f"Disposable checkout fixture setup failed: {exc}"))

    if order is not None and user is not None and validation_package is not None and check_passed("payment_checkout.admin_replay.fixtures"):
        try:
            checkout_service = PaymentCheckoutService(billing_service)
            checkout_status = checkout_service.status()
            checkout_provider_used = requested_checkout_provider or str(checkout_status.get("provider") or "default")
            checkout_result = checkout_service.create_checkout(
                str(order.get("id") or ""),
                {"role": "user", "user_id": user.get("id"), "email": user.get("email")},
                provider=requested_checkout_provider,
                metadata={
                    "source": "admin-checkout-webhook-replay",
                    "webhook_provider": normalized_webhook_provider,
                    "created_by": actor.get("user_id") or actor.get("id") or actor.get("key_id"),
                },
            )
            checkout = checkout_result.get("checkout") if isinstance(checkout_result.get("checkout"), dict) else {}
            checkout_order = checkout_result.get("order") if isinstance(checkout_result.get("order"), dict) else {}
            checkout_provider_used = str(checkout.get("provider") or requested_checkout_provider or "default")
            checkout_metadata = checkout_order.get("metadata") if isinstance(checkout_order.get("metadata"), dict) else {}
            stored_checkout = checkout_metadata.get("checkout") if isinstance(checkout_metadata.get("checkout"), dict) else {}
            checkout_available = bool(checkout.get("payment_url") or checkout.get("instructions") or checkout.get("provider_session_id"))
            checkout_ok = (
                bool(checkout.get("id"))
                and checkout.get("order_id") == order.get("id")
                and checkout_order.get("id") == order.get("id")
                and checkout_order.get("status") in {"created", "pending_payment"}
                and stored_checkout.get("id") == checkout.get("id")
                and checkout_available
            )
            checks.append(
                _safe_check(
                    "payment_checkout.admin_replay.checkout",
                    checkout_ok,
                    "Checkout session/link was created for the disposable order" if checkout_ok else "Checkout session/link validation failed",
                    {
                        "checkout_id": checkout.get("id"),
                        "checkout_provider": checkout_provider_used,
                        "checkout_mode": checkout.get("mode"),
                        "order_status": checkout_order.get("status"),
                        "has_payment_url": bool(checkout.get("payment_url")),
                        "has_instructions": bool(checkout.get("instructions")),
                        "has_provider_session_id": bool(checkout.get("provider_session_id")),
                    },
                )
            )
        except PaymentCheckoutError as exc:
            checks.append(_safe_check("payment_checkout.admin_replay.checkout", False, str(exc)))
        except ValueError as exc:
            checks.append(_safe_check("payment_checkout.admin_replay.checkout", False, str(exc)))

        if check_passed("payment_checkout.admin_replay.checkout"):
            paid_payment_id = f"admin_checkout_paid_{order.get('id')}_{now}"
            try:
                paid_payload = build_webhook_payload(
                    provider=normalized_webhook_provider,
                    action="paid",
                    order_id=str(order.get("id") or ""),
                    amount_cents=amount_cents,
                    currency=currency,
                    provider_payment_id=paid_payment_id,
                    event_id=f"evt_admin_checkout_paid_{now}",
                )
                paid_body = canonical_webhook_body(paid_payload)
                paid_headers = sign_webhook_headers(provider=normalized_webhook_provider, secret=normalized_secret, body=paid_body)
                paid_result = payment_webhook_service.handle(normalized_webhook_provider, paid_body, paid_headers, secret_override=normalized_secret)
                paid_order = paid_result.get("order") if isinstance(paid_result.get("order"), dict) else {}
                paid_payment = paid_result.get("payment") if isinstance(paid_result.get("payment"), dict) else {}
                paid_ok = (
                    paid_result.get("ok") is True
                    and paid_result.get("ignored") is False
                    and paid_result.get("action") == "mark_paid"
                    and paid_order.get("id") == order.get("id")
                    and paid_order.get("status") in {"paid", "fulfilled"}
                    and paid_payment.get("status") == "succeeded"
                )
                checks.append(
                    _safe_check(
                        "payment_checkout.admin_replay.paid",
                        paid_ok,
                        "Signed paid webhook replay fulfilled the disposable checkout order" if paid_ok else "Signed paid webhook replay failed for the checkout order",
                        {
                            "order_status": paid_order.get("status"),
                            "payment_status": paid_payment.get("status"),
                            "action": paid_result.get("action"),
                        },
                    )
                )
            except PaymentWebhookError as exc:
                checks.append(_safe_check("payment_checkout.admin_replay.paid", False, str(exc)))
            except ValueError as exc:
                checks.append(_safe_check("payment_checkout.admin_replay.paid", False, str(exc)))
        else:
            checks.append(_safe_check("payment_checkout.admin_replay.paid", False, "Paid webhook replay skipped because checkout creation failed"))

        if run_refund:
            if check_passed("payment_checkout.admin_replay.paid"):
                try:
                    paid_payment = paid_result.get("payment") if isinstance((paid_result or {}).get("payment"), dict) else {}
                    refund_payload = build_webhook_payload(
                        provider=normalized_webhook_provider,
                        action="refund",
                        order_id=str(order.get("id") or ""),
                        amount_cents=amount_cents,
                        currency=currency,
                        provider_payment_id=str(paid_payment.get("provider_payment_id") or f"admin_checkout_paid_{order.get('id')}_{now}"),
                        event_id=f"evt_admin_checkout_refund_{now}",
                    )
                    refund_body = canonical_webhook_body(refund_payload)
                    refund_headers = sign_webhook_headers(provider=normalized_webhook_provider, secret=normalized_secret, body=refund_body)
                    refund_result = payment_webhook_service.handle(normalized_webhook_provider, refund_body, refund_headers, secret_override=normalized_secret)
                    refund_order = refund_result.get("order") if isinstance(refund_result.get("order"), dict) else {}
                    refund_payment = refund_result.get("payment") if isinstance(refund_result.get("payment"), dict) else {}
                    refund_user = refund_result.get("user") if isinstance(refund_result.get("user"), dict) else {}
                    refund_ok = (
                        refund_result.get("ok") is True
                        and refund_result.get("ignored") is False
                        and refund_result.get("action") == "refund"
                        and refund_order.get("id") == order.get("id")
                        and refund_order.get("status") == "refunded"
                        and refund_payment.get("status") == "refunded"
                    )
                    checks.append(
                        _safe_check(
                            "payment_checkout.admin_replay.refund",
                            refund_ok,
                            "Signed refund webhook replay refunded the disposable checkout order" if refund_ok else "Signed refund webhook replay failed for the checkout order",
                            {
                                "order_status": refund_order.get("status"),
                                "payment_status": refund_payment.get("status"),
                                "action": refund_result.get("action"),
                                "quota_deducted": refund_result.get("quota_deducted"),
                                "quota_balance": refund_user.get("quota_balance"),
                            },
                        )
                    )
                except PaymentWebhookError as exc:
                    checks.append(_safe_check("payment_checkout.admin_replay.refund", False, str(exc)))
                except ValueError as exc:
                    checks.append(_safe_check("payment_checkout.admin_replay.refund", False, str(exc)))
            else:
                checks.append(_safe_check("payment_checkout.admin_replay.refund", False, "Refund replay skipped because paid replay failed"))

    cleanup_detail: dict[str, object] = {}
    try:
        if validation_package is not None:
            disabled_package = redemption_service.update_package(str(validation_package.get("id") or ""), {"enabled": False})
            package_disabled = bool(disabled_package and not disabled_package.get("enabled"))
            cleanup_detail["package_disabled"] = package_disabled
        if user is not None:
            disabled_user = auth_service.update_user(str(user.get("id") or ""), {"enabled": False})
            user_disabled = bool(disabled_user and not disabled_user.get("enabled"))
            cleanup_detail["user_disabled"] = user_disabled
            cleanup_detail["quota_balance"] = disabled_user.get("quota_balance") if disabled_user else None
        cleanup_needed = validation_package is not None or user is not None
        cleanup_ok = (not cleanup_needed) or (
            (validation_package is None or package_disabled)
            and (user is None or user_disabled)
        )
        checks.append(
            _safe_check(
                "payment_checkout.admin_replay.cleanup",
                cleanup_ok,
                "Disposable checkout validation fixtures were disabled" if cleanup_ok else "Disposable checkout validation cleanup failed",
                cleanup_detail,
            )
        )
    except Exception as exc:
        checks.append(_safe_check("payment_checkout.admin_replay.cleanup", False, str(exc), cleanup_detail))

    failed = [item for item in checks if item.get("status") == "failed"]
    passed = [item for item in checks if item.get("status") == "passed"]
    return {
        "status": "failed" if failed else "passed",
        "ready": not failed,
        "source": "admin-checkout-webhook-replay",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "provider": normalized_webhook_provider,
        "webhook_provider": normalized_webhook_provider,
        "checkout_provider": checkout_provider_used,
        "order_id": order.get("id") if order else None,
        "user_id": user.get("id") if user else None,
        "package_id": validation_package.get("id") if validation_package else None,
        "checkout_id": checkout.get("id"),
        "summary": {
            "total": len(checks),
            "passed": len(passed),
            "warning": 0,
            "failed": len(failed),
        },
        "evidence": {
            "payment_checkout_initiation_requested": True,
            "payment_checkout_order_created": check_passed("payment_checkout.admin_replay.fixtures") and order is not None,
            "payment_checkout_session_created": check_passed("payment_checkout.admin_replay.checkout"),
            "payment_checkout_webhook_replay_requested": True,
            "payment_checkout_paid_replay": check_passed("payment_checkout.admin_replay.paid"),
            "payment_checkout_refund_replay": check_passed("payment_checkout.admin_replay.refund"),
            "disposable_order_id": order.get("id") if order else None,
            "disposable_user_disabled": user_disabled,
            "temporary_package_disabled": package_disabled,
            "checkout_id": checkout.get("id"),
            "checkout_provider": checkout_provider_used,
            "webhook_provider": normalized_webhook_provider,
        },
        "checks": checks,
    }


def create_router(app_version: str) -> APIRouter:
    router = APIRouter()

    @router.get("/health/live")
    async def health_live():
        return monitoring_service.live(version=app_version)

    @router.get("/health/ready")
    async def health_ready():
        readiness = monitoring_service.readiness()
        status_code = 503 if readiness.get("status") == "unhealthy" else 200
        return JSONResponse(readiness, status_code=status_code)

    @router.get("/auth/capabilities")
    async def auth_capabilities():
        email_status = email_service.status()
        return {
            "registration_enabled": REGISTRATION_ENABLED,
            "email_verification_required": EMAIL_VERIFICATION_REQUIRED,
            "session_cookie_enabled": _auth_cookie_enabled(),
            "password_reset_enabled": True,
            "email_delivery_configured": bool(email_status.get("configured")),
            "email_provider": email_status.get("provider"),
        }

    @router.post("/auth/login")
    async def login(
            request: Request,
            response: Response,
            body: AuthLoginRequest | None = None,
            authorization: str | None = Header(default=None),
            session_cookie: str | None = Cookie(default=None, alias=AUTH_SESSION_COOKIE_NAME),
    ):
        if body and body.email.strip() and body.password:
            _check_login_rate_limit(body.email, request)
            try:
                user, token, key = auth_service.login_user(body.email, body.password)
                _clear_login_attempts(body.email, request)
            except ValueError:
                raise HTTPException(status_code=401, detail={"error": "email or password is invalid"}) from None
            if EMAIL_VERIFICATION_REQUIRED and not user.get("email_verified_at"):
                raise HTTPException(status_code=403, detail={"error": "email verification required"})
            identity = {**key, **user, "user_id": user.get("id"), "key_id": key.get("id")}
            cookie_set = _set_session_cookie(response, identity)
            return _session_response(app_version, identity, token, session_cookie=cookie_set)
        effective_authorization = _authorization_with_cookie(authorization, session_cookie)
        _check_key_login_rate_limit(effective_authorization, request)
        identity = require_identity(effective_authorization)
        cookie_set = _set_session_cookie(response, identity)
        return _session_response(app_version, identity, session_cookie=cookie_set)

    @router.post("/auth/register")
    async def register(request: Request, body: AuthRegisterRequest, response: Response = None):
        response = response or Response()
        if not REGISTRATION_ENABLED:
            raise HTTPException(status_code=403, detail={"error": "registration is disabled"})
        _check_register_rate_limit(body.email, request)
        try:
            user, token, key = auth_service.register_user(body.email, body.password, body.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        identity = {**key, **user, "user_id": user.get("id"), "key_id": key.get("id")}
        verification_item, verification_token = auth_service.create_email_verification_token(str(user.get("id") or ""))
        email_delivery = await _send_action_email("email_verify", verification_item, verification_token)
        cookie_set = False if EMAIL_VERIFICATION_REQUIRED else _set_session_cookie(response, identity)
        payload = _session_response(app_version, identity, None if EMAIL_VERIFICATION_REQUIRED else token, session_cookie=cookie_set)
        payload["verification_required"] = EMAIL_VERIFICATION_REQUIRED
        verification_payload = _verification_token_payload(
            verification_item,
            verification_token,
            email_delivery=email_delivery,
        )
        payload["verification_expires_at"] = verification_payload.get("expires_at")
        payload["email_sent"] = verification_payload.get("email_sent")
        payload["email_provider"] = verification_payload.get("email_provider")
        payload["email_delivery"] = verification_payload.get("email_delivery")
        if verification_payload.get("token"):
            payload["verification_token"] = verification_payload.get("token")
        return payload

    @router.get("/auth/me")
    async def get_auth_identity(
            authorization: str | None = Header(default=None),
            session_cookie: str | None = Cookie(default=None, alias=AUTH_SESSION_COOKIE_NAME),
    ):
        return _public_identity(require_identity(_authorization_with_cookie(authorization, session_cookie)))

    @router.post("/auth/logout")
    async def logout(response: Response, authorization: str | None = Header(default=None), session_cookie: str | None = Cookie(default=None, alias=AUTH_SESSION_COOKIE_NAME)):
        token = str(session_cookie or "").strip()
        if not token:
            from api.support import extract_bearer_token
            bearer = extract_bearer_token(authorization)
            token = bearer if bearer.startswith("sess-") else ""
        if token:
            auth_service.revoke_session(token)
        _clear_session_cookie(response)
        return {"ok": True}

    @router.post("/auth/email/verification/request")
    async def request_email_verification(request: Request, body: AuthEmailVerificationRequest):
        _check_register_rate_limit(body.email, request)
        try:
            item, token = auth_service.create_email_verification_token(body.email)
        except ValueError:
            # Enumeration-safe public response; no token returned when account is absent.
            return {"ok": True, "sent": True}
        email_delivery = await _send_action_email("email_verify", item, token)
        return _verification_token_payload(item, token, email_delivery=email_delivery)

    @router.post("/auth/email/verification/confirm")
    async def confirm_email_verification(body: AuthTokenConfirmRequest):
        try:
            user = auth_service.verify_email_token(body.token)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        return {"ok": True, "user": user}

    @router.post("/auth/password/reset/request")
    async def request_password_reset(request: Request, body: AuthPasswordResetRequest):
        _check_login_rate_limit(body.email, request)
        item, token = auth_service.create_password_reset_token(body.email)
        # Enumeration-safe: always ok even when email is not registered.
        if item is None:
            return {"ok": True, "sent": True}
        email_delivery = await _send_action_email("password_reset", item, token)
        return _verification_token_payload(item, token, email_delivery=email_delivery)

    @router.post("/auth/password/reset/confirm")
    async def confirm_password_reset(response: Response, body: AuthPasswordResetConfirmRequest):
        try:
            user = auth_service.reset_password_with_token(body.token, body.password)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        _clear_session_cookie(response)
        return {"ok": True, "user": user}

    @router.post("/auth/redeem")
    async def redeem_code(
            request: Request,
            body: RedeemRequest,
            authorization: str | None = Header(default=None),
            session_cookie: str | None = Cookie(default=None, alias=AUTH_SESSION_COOKIE_NAME),
    ):
        _check_redeem_rate_limit(None, request)
        identity = require_identity(_authorization_with_cookie(authorization, session_cookie))
        _check_redeem_rate_limit(identity, request)
        try:
            result = redemption_service.redeem(body.code, identity)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        return {"ok": True, **result}

    @router.get("/api/me")
    async def get_current_identity(
            authorization: str | None = Header(default=None),
            session_cookie: str | None = Cookie(default=None, alias=AUTH_SESSION_COOKIE_NAME),
    ):
        return _public_identity(require_identity(_authorization_with_cookie(authorization, session_cookie)))

    @router.get("/api/me/quota-ledger")
    async def get_my_quota_ledger(
            limit: int = Query(default=100, ge=1, le=500),
            authorization: str | None = Header(default=None),
            session_cookie: str | None = Cookie(default=None, alias=AUTH_SESSION_COOKIE_NAME),
    ):
        identity = require_identity(_authorization_with_cookie(authorization, session_cookie))
        user_id = str(identity.get("user_id") or "").strip()
        if not user_id:
            return {"items": []}
        return {"items": auth_service.list_quota_ledger(user_id=user_id, limit=limit)}

    @router.get("/version")
    async def get_version():
        return {"version": app_version}

    @router.get("/api/settings")
    async def get_settings(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"config": config.get()}

    @router.post("/api/settings")
    async def save_settings(body: SettingsUpdateRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"config": config.update(body.model_dump(mode="python"))}

    @router.get("/api/images")
    async def get_images(request: Request, start_date: str = "", end_date: str = "", authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return list_images(resolve_image_base_url(request), start_date=start_date.strip(), end_date=end_date.strip())

    @router.get("/api/logs")
    async def get_logs(type: str = "", start_date: str = "", end_date: str = "", authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"items": log_service.list(type=type.strip(), start_date=start_date.strip(), end_date=end_date.strip())}

    @router.get("/api/admin/audit-logs")
    async def get_audit_logs(
            action: str = "",
            actor_id: str = "",
            target_type: str = "",
            target_id: str = "",
            start_date: str = "",
            end_date: str = "",
            limit: int = Query(default=200, ge=1, le=1000),
            authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        return {
            "items": audit_service.list_logs(
                action=action or None,
                actor_id=actor_id or None,
                target_type=target_type or None,
                target_id=target_id or None,
                start_date=start_date.strip(),
                end_date=end_date.strip(),
                limit=limit,
            )
        }

    @router.get("/api/admin/metrics")
    async def get_admin_metrics(
            format: str = Query(default="json"),
            authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        snapshot = monitoring_service.collect()
        if format.strip().lower() in {"prometheus", "prom", "text"}:
            return PlainTextResponse(monitoring_service.prometheus_text(snapshot), media_type="text/plain; version=0.0.4")
        return snapshot

    @router.get("/api/admin/alerts")
    async def get_admin_alerts(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        snapshot = monitoring_service.collect()
        return {
            "status": snapshot.get("status"),
            "alerts": snapshot.get("alerts") or [],
            "time": snapshot.get("time"),
        }

    @router.get("/api/admin/business-report")
    async def get_business_report(
            days: int = Query(default=30, ge=1, le=3660),
            authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        return await run_in_threadpool(reporting_service.collect, days=days)

    @router.get("/api/admin/production-readiness")
    async def get_production_readiness(
            strict: bool = Query(default=True),
            authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        return await run_in_threadpool(production_readiness_service.check, strict=strict)

    @router.get("/api/admin/launch-evidence")
    async def list_launch_evidence(
            limit: int = Query(default=50, ge=1, le=500),
            authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        return {"items": await run_in_threadpool(launch_evidence_service.list, limit=limit)}

    @router.post("/api/admin/launch-evidence")
    async def create_launch_evidence(body: LaunchEvidenceCreateRequest, authorization: str | None = Header(default=None)):
        actor = require_admin(authorization)
        try:
            item = await run_in_threadpool(
                launch_evidence_service.create,
                body.report,
                actor=actor,
                name=body.name,
                source=body.source,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        return {"item": item}

    @router.post("/api/admin/payment-webhook/replay")
    async def replay_payment_webhook(body: PaymentWebhookReplayRequest, authorization: str | None = Header(default=None)):
        actor = require_admin(authorization)
        try:
            report = await run_in_threadpool(
                _payment_webhook_replay_report,
                actor=actor,
                provider=body.provider,
                secret=body.secret,
                package_id=body.package_id,
                amount_cents=body.amount_cents,
                currency=body.currency,
                quota=body.quota,
                run_refund=body.run_refund,
                email=body.email,
            )
            evidence_item = None
            if body.archive:
                evidence_item = await run_in_threadpool(
                    launch_evidence_service.create,
                    report,
                    actor=actor,
                    name=body.evidence_name or f"payment webhook replay {report.get('provider')} {report.get('generated_at')}",
                    source="admin-payment-webhook-replay",
                )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        return {"report": report, "item": evidence_item}

    @router.post("/api/admin/checkout-webhook/replay")
    async def replay_checkout_webhook(body: CheckoutWebhookReplayRequest, authorization: str | None = Header(default=None)):
        actor = require_admin(authorization)
        try:
            report = await run_in_threadpool(
                _checkout_webhook_replay_report,
                actor=actor,
                checkout_provider=body.checkout_provider,
                webhook_provider=body.webhook_provider,
                secret=body.secret,
                amount_cents=body.amount_cents,
                currency=body.currency,
                quota=body.quota,
                run_refund=body.run_refund,
                email=body.email,
            )
            evidence_item = None
            if body.archive:
                evidence_item = await run_in_threadpool(
                    launch_evidence_service.create,
                    report,
                    actor=actor,
                    name=body.evidence_name or f"checkout webhook replay {report.get('checkout_provider')} {report.get('webhook_provider')} {report.get('generated_at')}",
                    source="admin-checkout-webhook-replay",
                )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        return {"report": report, "item": evidence_item}

    @router.get("/api/admin/launch-evidence/{evidence_id}")
    async def get_launch_evidence(evidence_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        item = await run_in_threadpool(launch_evidence_service.get, evidence_id)
        if item is None:
            raise HTTPException(status_code=404, detail={"error": "launch evidence not found"})
        return {"item": item}

    @router.delete("/api/admin/launch-evidence/{evidence_id}")
    async def delete_launch_evidence(evidence_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        deleted = await run_in_threadpool(launch_evidence_service.delete, evidence_id)
        if not deleted:
            raise HTTPException(status_code=404, detail={"error": "launch evidence not found"})
        return {"ok": True}

    @router.post("/api/proxy/test")
    async def test_proxy_endpoint(body: ProxyTestRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        candidate = (body.url or "").strip() or config.get_proxy_settings()
        if not candidate:
            raise HTTPException(status_code=400, detail={"error": "proxy url is required"})
        return {"result": await run_in_threadpool(test_proxy, candidate)}

    @router.get("/api/storage/info")
    async def get_storage_info(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        storage = config.get_storage_backend()
        object_storage = config.get_object_storage_backend()
        from api import support as support_api
        from services.image_job_service import image_job_service
        return {
            "backend": storage.get_backend_info(),
            "health": storage.health_check(),
            "object_storage": object_storage.info(),
            "image_job_queue": image_job_service.queue_info(),
            "rate_limit": {
                "public_actions": _public_rate_limiter.info(),
                "api_keys": support_api._api_rate_limiter.info(),
            },
        }

    return router

