from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Mapping

from services.billing_service import BillingService, billing_service
from services.config import config


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(value: object) -> str:
    return str(value or "").strip()


def _is_placeholder(value: object) -> bool:
    raw = _clean(value).lower()
    if not raw:
        return True
    return any(marker in raw for marker in ("change-me", "your_", "example", "placeholder", "secret_key_here"))


def _env_bool(name: str, default: bool = False, env: Mapping[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    raw = source.get(name)
    if raw is None:
        return default
    return _clean(raw).lower() not in {"0", "false", "no", "off"}


def _origin(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def _is_public_https_url(value: str) -> bool:
    parsed = urllib.parse.urlsplit(_clean(value))
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and bool(host) and host not in {"localhost", "127.0.0.1", "::1"} and not host.endswith(".localhost")


class PaymentCheckoutError(ValueError):
    pass


class PaymentCheckoutService:
    """Create payable checkout sessions for pending commercial orders.

    The existing webhook path remains the source of truth for fulfillment; this
    service only creates and stores a checkout attempt so customers can be sent
    to a payment page or shown operator payment instructions.
    """

    def __init__(
        self,
        billing: BillingService | None,
        *,
        env: Mapping[str, str] | None = None,
        config_obj: Any = None,
    ):
        self.billing = billing
        self.env = env if env is not None else os.environ
        self.config = config_obj or config

    def status(self) -> dict[str, Any]:
        provider = self._provider()
        status: dict[str, Any] = {
            "provider": provider,
            "enabled": provider != "disabled",
            "configured": False,
            "message": "",
        }
        if provider == "disabled":
            status["message"] = "payment checkout is disabled"
            return status
        if provider == "manual":
            has_instructions = bool(self._env("PAYMENT_CHECKOUT_MANUAL_INSTRUCTIONS") or self._env("PAYMENT_CHECKOUT_MANUAL_URL"))
            status.update({
                "configured": has_instructions,
                "message": "manual payment instructions are configured" if has_instructions else "manual checkout needs instructions or a payment URL",
            })
            return status
        if provider == "redirect":
            template = self._env("PAYMENT_CHECKOUT_URL_TEMPLATE")
            secret = self._checkout_secret()
            configured = bool(template) and _is_public_https_url(template.split("{", 1)[0] or template) and not _is_placeholder(secret)
            status.update({
                "configured": configured,
                "message": "signed redirect checkout template is configured" if configured else "PAYMENT_CHECKOUT_URL_TEMPLATE and PAYMENT_CHECKOUT_SIGNING_SECRET are required",
            })
            return status
        if provider == "stripe":
            api_key = self._env("STRIPE_SECRET_KEY")
            url_ok = _is_public_https_url(self._default_success_url("ord_check")) and _is_public_https_url(self._default_cancel_url("ord_check"))
            configured = bool(api_key) and not _is_placeholder(api_key) and url_ok
            status.update({
                "configured": configured,
                "message": "Stripe Checkout API key and return URLs are configured" if configured else "STRIPE_SECRET_KEY plus public success/cancel URLs are required",
            })
            return status
        status["message"] = f"unsupported payment checkout provider: {provider}"
        return status

    def create_checkout(
        self,
        order_id: str,
        identity: dict[str, object],
        *,
        provider: str = "",
        success_url: str = "",
        cancel_url: str = "",
        metadata: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        if self.billing is None:
            raise PaymentCheckoutError("payment checkout service is not bound to billing")
        order = self.billing.get_order(order_id, identity)
        if order is None:
            raise PaymentCheckoutError("order not found")
        if _clean(order.get("status")) not in {"created", "pending_payment"}:
            raise PaymentCheckoutError("only unpaid orders can create checkout")
        amount_cents = max(0, int(order.get("amount_cents") or 0))
        if amount_cents <= 0:
            raise PaymentCheckoutError("order amount must be greater than zero")

        normalized_provider = self._normalize_provider(provider or self._provider())
        if normalized_provider == "disabled":
            raise PaymentCheckoutError("payment checkout is disabled")
        if normalized_provider == "manual":
            checkout = self._manual_checkout(order, metadata=metadata)
        elif normalized_provider == "redirect":
            checkout = self._redirect_checkout(order, success_url=success_url, cancel_url=cancel_url, metadata=metadata)
        elif normalized_provider == "stripe":
            checkout = self._stripe_checkout(order, success_url=success_url, cancel_url=cancel_url, metadata=metadata)
        else:
            raise PaymentCheckoutError(f"unsupported payment checkout provider: {normalized_provider}")

        updated_order = self.billing.attach_checkout_session(order_id, identity, checkout)
        if updated_order is None:
            raise PaymentCheckoutError("order not found")
        return {"order": updated_order, "checkout": checkout}

    def _env(self, name: str) -> str:
        return _clean(self.env.get(name, ""))

    def _provider(self) -> str:
        return self._normalize_provider(self._env("PAYMENT_CHECKOUT_PROVIDER") or "manual")

    @staticmethod
    def _normalize_provider(provider: str) -> str:
        normalized = _clean(provider).lower().replace("-", "_")
        aliases = {
            "off": "disabled",
            "none": "disabled",
            "custom": "redirect",
            "external": "redirect",
            "external_url": "redirect",
            "stripe_checkout": "stripe",
        }
        return aliases.get(normalized, normalized or "manual")

    def _checkout_secret(self) -> str:
        return self._env("PAYMENT_CHECKOUT_SIGNING_SECRET") or self._env("PAYMENT_WEBHOOK_SECRET")

    def _public_base_url(self) -> str:
        return (
            self._env("APP_PUBLIC_URL")
            or self._env("CHATGPT2API_BASE_URL")
            or _clean(getattr(self.config, "base_url", ""))
        ).rstrip("/")

    def _default_success_url(self, order_id: str) -> str:
        configured = self._env("PAYMENT_CHECKOUT_SUCCESS_URL")
        if configured:
            return self._format_url_template(configured, {"order_id": order_id, "checkout_status": "success"})
        base = self._public_base_url()
        return f"{base}/redeem?checkout=success&order_id={urllib.parse.quote(order_id)}" if base else ""

    def _default_cancel_url(self, order_id: str) -> str:
        configured = self._env("PAYMENT_CHECKOUT_CANCEL_URL")
        if configured:
            return self._format_url_template(configured, {"order_id": order_id, "checkout_status": "cancel"})
        base = self._public_base_url()
        return f"{base}/redeem?checkout=cancel&order_id={urllib.parse.quote(order_id)}" if base else ""

    def _allowed_return_origins(self) -> set[str]:
        origins = {_origin(self._public_base_url())}
        for value in getattr(self.config, "web_allowed_origins", []) or []:
            origins.add(_origin(str(value)))
        for value in self._env("WEB_ALLOWED_ORIGINS").split(","):
            origins.add(_origin(value.strip()))
        return {item for item in origins if item}

    def _safe_return_url(self, value: str, fallback: str) -> str:
        candidate = _clean(value)
        if not candidate:
            return fallback
        candidate_origin = _origin(candidate)
        if candidate_origin and candidate_origin in self._allowed_return_origins():
            return candidate
        return fallback

    def _base_checkout_payload(self, order: dict[str, object], *, provider: str, mode: str, metadata: dict[str, object] | None = None) -> dict[str, Any]:
        now = _now_iso()
        seed = f"{provider}:{order.get('id')}:{now}"
        return {
            "id": f"chk_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:24]}",
            "provider": provider,
            "mode": mode,
            "order_id": order.get("id"),
            "amount_cents": order.get("amount_cents"),
            "currency": order.get("currency"),
            "status": "created",
            "created_at": now,
            "metadata": {str(key): value for key, value in (metadata or {}).items() if str(key).strip()},
        }

    def _manual_checkout(self, order: dict[str, object], *, metadata: dict[str, object] | None = None) -> dict[str, Any]:
        checkout = self._base_checkout_payload(order, provider="manual", mode="manual", metadata=metadata)
        instructions = self._env("PAYMENT_CHECKOUT_MANUAL_INSTRUCTIONS") or "请按页面提示完成转账，并在备注中填写订单号。"
        payment_url = self._env("PAYMENT_CHECKOUT_MANUAL_URL")
        qr_code_url = self._env("PAYMENT_CHECKOUT_QR_CODE_URL")
        checkout.update({
            "instructions": self._format_url_template(instructions, self._template_values(order)),
            "payment_url": self._format_url_template(payment_url, self._template_values(order)) if payment_url else None,
            "qr_code_url": self._format_url_template(qr_code_url, self._template_values(order)) if qr_code_url else None,
        })
        return checkout

    def _redirect_checkout(
        self,
        order: dict[str, object],
        *,
        success_url: str = "",
        cancel_url: str = "",
        metadata: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        template = self._env("PAYMENT_CHECKOUT_URL_TEMPLATE")
        if not template:
            raise PaymentCheckoutError("PAYMENT_CHECKOUT_URL_TEMPLATE is required")
        success = self._safe_return_url(success_url, self._default_success_url(str(order.get("id") or "")))
        cancel = self._safe_return_url(cancel_url, self._default_cancel_url(str(order.get("id") or "")))
        values = self._template_values(order, success_url=success, cancel_url=cancel)
        payment_url = self._format_url_template(template, values)
        checkout = self._base_checkout_payload(order, provider="redirect", mode="redirect", metadata=metadata)
        checkout.update({
            "payment_url": payment_url,
            "success_url": success,
            "cancel_url": cancel,
            "signature": values.get("signature"),
        })
        return checkout

    def _stripe_checkout(
        self,
        order: dict[str, object],
        *,
        success_url: str = "",
        cancel_url: str = "",
        metadata: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        api_key = self._env("STRIPE_SECRET_KEY")
        if not api_key or _is_placeholder(api_key):
            raise PaymentCheckoutError("STRIPE_SECRET_KEY is required")
        order_id = str(order.get("id") or "")
        success = self._safe_return_url(success_url, self._default_success_url(order_id))
        cancel = self._safe_return_url(cancel_url, self._default_cancel_url(order_id))
        if not _is_public_https_url(success) or not _is_public_https_url(cancel):
            raise PaymentCheckoutError("Stripe checkout requires public HTTPS success and cancel URLs")

        session = self._create_stripe_session(order, api_key=api_key, success_url=success, cancel_url=cancel, metadata=metadata)
        checkout = self._base_checkout_payload(order, provider="stripe", mode="hosted", metadata=metadata)
        checkout.update({
            "provider_session_id": session.get("id"),
            "payment_url": session.get("url"),
            "success_url": success,
            "cancel_url": cancel,
            "expires_at": session.get("expires_at"),
            "provider_payment_status": session.get("payment_status"),
            "livemode": session.get("livemode"),
        })
        return checkout

    def _create_stripe_session(
        self,
        order: dict[str, object],
        *,
        api_key: str,
        success_url: str,
        cancel_url: str,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        order_id = str(order.get("id") or "")
        package_name = _clean(order.get("package_name") or order.get("package_id") or "Image quota")
        currency = _clean(order.get("currency") or "CNY").lower()
        amount_cents = max(0, int(order.get("amount_cents") or 0))
        quantity = max(1, int(order.get("quantity") or 1))
        form: list[tuple[str, str]] = [
            ("mode", "payment"),
            ("client_reference_id", order_id),
            ("success_url", success_url),
            ("cancel_url", cancel_url),
            ("line_items[0][quantity]", str(quantity)),
            ("line_items[0][price_data][currency]", currency),
            ("line_items[0][price_data][unit_amount]", str(max(1, amount_cents // quantity))),
            ("line_items[0][price_data][product_data][name]", package_name[:120]),
            ("metadata[order_id]", order_id),
            ("metadata[user_id]", str(order.get("user_id") or "")),
            ("payment_intent_data[metadata][order_id]", order_id),
            ("payment_intent_data[metadata][user_id]", str(order.get("user_id") or "")),
        ]
        email = _clean(order.get("email"))
        if email:
            form.append(("customer_email", email))
        for key, value in (metadata or {}).items():
            safe_key = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in str(key).strip())[:40]
            if safe_key:
                form.append((f"metadata[{safe_key}]", str(value)[:500]))

        data = urllib.parse.urlencode(form).encode("utf-8")
        endpoint = self._env("STRIPE_CHECKOUT_API_URL") or "https://api.stripe.com/v1/checkout/sessions"
        token = base64.b64encode(f"{api_key}:".encode("utf-8")).decode("ascii")
        request = urllib.request.Request(
            endpoint,
            data=data,
            headers={
                "Authorization": f"Basic {token}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Idempotency-Key": f"checkout:{order_id}",
            },
            method="POST",
        )
        timeout = max(1, int(self._env("PAYMENT_CHECKOUT_HTTP_TIMEOUT_SECONDS") or "15"))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - configured payment provider endpoint
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8")
            except Exception:
                detail = str(exc)
            raise PaymentCheckoutError(f"Stripe checkout request failed: {detail[:500]}") from exc
        except Exception as exc:
            raise PaymentCheckoutError(f"Stripe checkout request failed: {exc}") from exc
        if not isinstance(payload, dict) or not payload.get("id") or not payload.get("url"):
            raise PaymentCheckoutError("Stripe checkout response did not include session id/url")
        return payload

    def _template_values(self, order: dict[str, object], *, success_url: str = "", cancel_url: str = "") -> dict[str, str]:
        order_id = str(order.get("id") or "")
        values = {
            "order_id": order_id,
            "user_id": str(order.get("user_id") or ""),
            "email": str(order.get("email") or ""),
            "package_id": str(order.get("package_id") or ""),
            "package_name": str(order.get("package_name") or ""),
            "amount_cents": str(order.get("amount_cents") or 0),
            "currency": str(order.get("currency") or "CNY"),
            "quantity": str(order.get("quantity") or 1),
            "success_url": success_url,
            "cancel_url": cancel_url,
            "notify_url": f"{self._public_base_url()}/api/payments/webhook/redirect" if self._public_base_url() else "",
        }
        secret = self._checkout_secret()
        if secret and not _is_placeholder(secret):
            signed = ".".join([values["order_id"], values["amount_cents"], values["currency"], values["user_id"]])
            values["signature"] = hmac.new(secret.encode("utf-8"), signed.encode("utf-8"), hashlib.sha256).hexdigest()
        else:
            values["signature"] = ""
        for key, value in list(values.items()):
            values[f"{key}_raw"] = value
            values[key] = urllib.parse.quote(value, safe="")
        return values

    @staticmethod
    def _format_url_template(template: str, values: dict[str, str]) -> str:
        if not template:
            return ""
        try:
            return template.format(**values)
        except KeyError:
            return template


payment_checkout_service = PaymentCheckoutService(billing_service)
