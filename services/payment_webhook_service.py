from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from services.billing_service import BillingService, billing_service


def _clean(value: object) -> str:
    return str(value or "").strip()


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_non_negative_int(value: object, default: int = 0) -> int:
    return max(0, _safe_int(value, default))


def _currency(value: object) -> str:
    normalized = _clean(value).upper() or "CNY"
    normalized = "".join(ch for ch in normalized if ch.isalnum())
    return normalized[:8] or "CNY"


def _provider_env_name(provider: str) -> str:
    safe_provider = "".join(ch if ch.isalnum() else "_" for ch in _clean(provider).upper()).strip("_")
    return f"PAYMENT_WEBHOOK_SECRET_{safe_provider}" if safe_provider else ""


def _header_map(headers: Mapping[str, str] | None) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in dict(headers or {}).items()}


def _nested_dict(value: object, *keys: str) -> dict[str, Any]:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}


def _nested_value(value: object, path: str) -> object:
    current = value
    for key in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _is_present(value: object) -> bool:
    return value is not None and value != ""


def _first_present(source: dict[str, Any], *keys: str) -> object:
    for key in keys:
        value = _nested_value(source, key) if "." in key else source.get(key)
        if _is_present(value):
            return value
    return None


def _first_present_from_sources(sources: list[dict[str, Any]], *keys: str) -> object:
    for source in sources:
        value = _first_present(source, *keys)
        if _is_present(value):
            return value
    return None


def _parse_amount_cents(value: object) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(round(value * 100)))
    text = _clean(value)
    if not text:
        return None
    if text.isdigit():
        return int(text)
    try:
        return max(0, int(round(float(text) * 100)))
    except ValueError:
        return None


def _normalize_event_type(value: object) -> str:
    normalized = _clean(value).lower()
    normalized = normalized.replace("/", ".").replace("-", ".")
    return normalized


class PaymentWebhookError(ValueError):
    """Base class for payment webhook errors."""


class PaymentWebhookSignatureError(PaymentWebhookError):
    """Raised when signature verification fails."""


class PaymentWebhookPayloadError(PaymentWebhookError):
    """Raised when the webhook payload cannot be parsed into a payment event."""


@dataclass(frozen=True)
class PaymentWebhookEvent:
    provider: str
    event_type: str
    event_id: str | None
    status: str
    order_id: str
    provider_payment_id: str | None
    idempotency_key: str
    amount_cents: int | None
    currency: str | None
    metadata: dict[str, object]
    should_mark_paid: bool
    should_refund: bool


class PaymentWebhookService:
    """Provider-agnostic signed payment webhook adapter.

    MVP contract for payment providers:

    - Sign the raw request body with HMAC-SHA256.
    - Send the signature in `X-Payment-Signature`, `X-Webhook-Signature`,
      `X-ChatGPT2API-Signature`, or `X-Signature`.
    - Recommended format is `t=<unix_seconds>,v1=<hex>`, where the signed
      message is `<timestamp>.<raw_body>`. Plain `sha256=<hex>` or raw hex over
      the body is also accepted for simple gateways.
    - Put the commercial order id in `order_id`, `out_trade_no`,
      `merchant_order_id`, `client_reference_id`, or `metadata.order_id`.
    """

    SUCCESS_EVENT_TYPES = {
        "checkout.session.completed",
        "charge.succeeded",
        "order.paid",
        "paid",
        "payment.completed",
        "payment.paid",
        "payment.success",
        "payment.succeeded",
        "payment_intent.succeeded",
        "trade.success",
        "trade_success",
        "transaction.success",
        "transaction.successful",
    }
    SUCCESS_STATUSES = {
        "completed",
        "paid",
        "success",
        "succeeded",
        "trade_finished",
        "trade_success",
    }
    REFUND_EVENT_TYPES = {
        "charge.refunded",
        "order.refunded",
        "payment.refund",
        "payment.refunded",
        "payment.refund.success",
        "refund.succeeded",
        "refund.success",
        "refunded",
        "trade.refund.success",
        "trade_refund_success",
    }

    PROVIDER_ALIASES = {
        "alipayplus": "alipay",
        "alipay-global": "alipay",
        "stripe-connect": "stripe",
        "wechat": "wechatpay",
        "wechat-pay": "wechatpay",
        "weixin": "wechatpay",
        "weixin-pay": "wechatpay",
    }
    REFUND_STATUSES = {
        "refund",
        "refund_success",
        "refund_succeeded",
        "refunded",
        "refunded_success",
        "trade_refund_success",
    }

    def __init__(self, billing: BillingService):
        self.billing_service = billing

    def handle(
        self,
        provider: str,
        body: bytes,
        headers: Mapping[str, str] | None = None,
        *,
        secret_override: str | None = None,
    ) -> dict[str, object]:
        normalized_provider = self._normalize_provider(provider)
        self.verify_signature(normalized_provider, body, headers, secret_override=secret_override)
        payload = self._decode_payload(body)
        event = self.parse_event(normalized_provider, payload)
        if event.should_refund:
            result = self.billing_service.refund_order(
                event.order_id,
                actor={"role": "system", "id": f"payment-webhook:{event.provider}"},
                reason=f"payment-webhook:{event.provider}",
                metadata=event.metadata,
            )
            return {
                "ok": True,
                "ignored": False,
                "action": "refund",
                "provider": event.provider,
                "event_type": event.event_type,
                "status": event.status,
                "order_id": event.order_id,
                "event_id": event.event_id,
                **result,
            }
        if not event.should_mark_paid:
            return {
                "ok": True,
                "ignored": True,
                "action": "ignore",
                "provider": event.provider,
                "event_type": event.event_type,
                "status": event.status,
                "order_id": event.order_id,
                "event_id": event.event_id,
            }
        result = self.billing_service.mark_paid(
            event.order_id,
            provider=event.provider,
            provider_payment_id=event.provider_payment_id,
            amount_cents=event.amount_cents,
            currency=event.currency,
            idempotency_key=event.idempotency_key,
            actor={"role": "system", "id": f"payment-webhook:{event.provider}"},
            metadata=event.metadata,
            auto_fulfill=True,
        )
        return {
            "ok": True,
            "ignored": False,
            "action": "mark_paid",
            "provider": event.provider,
            "event_type": event.event_type,
            "status": event.status,
            "order_id": event.order_id,
            "event_id": event.event_id,
            **result,
        }

    def verify_signature(
        self,
        provider: str,
        body: bytes,
        headers: Mapping[str, str] | None = None,
        *,
        secret_override: str | None = None,
    ) -> None:
        secret = _clean(secret_override) or self._secret_for_provider(provider)
        if not secret:
            raise PaymentWebhookSignatureError("payment webhook secret is not configured")
        header_values = _header_map(headers)
        signature_header = (
            header_values.get("stripe-signature")
            or header_values.get("x-payment-signature")
            or header_values.get("x-webhook-signature")
            or header_values.get("x-chatgpt2api-signature")
            or header_values.get("x-alipay-signature")
            or header_values.get("alipay-signature")
            or header_values.get("x-wechatpay-signature")
            or header_values.get("wechatpay-signature")
            or header_values.get("x-signature")
            or ""
        )
        if not signature_header:
            raise PaymentWebhookSignatureError("payment webhook signature is missing")

        timestamp = (
            header_values.get("x-payment-timestamp")
            or header_values.get("x-webhook-timestamp")
            or header_values.get("x-timestamp")
            or header_values.get("x-alipay-timestamp")
            or header_values.get("alipay-timestamp")
            or header_values.get("x-wechatpay-timestamp")
            or header_values.get("wechatpay-timestamp")
            or self._signature_part(signature_header, "t")
            or ""
        )
        if timestamp:
            self._check_timestamp(timestamp)

        candidates = self._signature_candidates(signature_header)
        if not candidates:
            raise PaymentWebhookSignatureError("payment webhook signature is invalid")

        expected_messages = [body]
        if timestamp:
            expected_messages.insert(0, f"{timestamp}.".encode("utf-8") + body)
        expected_signatures = [
            hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
            for message in expected_messages
        ]
        for candidate in candidates:
            normalized = candidate.lower()
            if any(hmac.compare_digest(normalized, expected) for expected in expected_signatures):
                return
        raise PaymentWebhookSignatureError("payment webhook signature mismatch")

    def parse_event(self, provider: str, payload: dict[str, Any]) -> PaymentWebhookEvent:
        event_type = _normalize_event_type(_first_present(payload, "type", "event_type", "eventType", "notify_type"))
        data = self._event_data(payload)
        metadata = _nested_dict(data, "metadata") or _nested_dict(payload, "metadata")
        amount = data.get("amount") if isinstance(data.get("amount"), dict) else {}
        refund_amount = data.get("refund_amount") if isinstance(data.get("refund_amount"), dict) else {}
        payer = data.get("payer") if isinstance(data.get("payer"), dict) else {}
        sources = [data, amount, refund_amount, payer, metadata, payload]

        status = _clean(_first_present_from_sources(
            sources,
            "status",
            "trade_status",
            "payment_status",
            "trade_state",
            "refund_status",
        )).lower()
        order_id = _clean(_first_present_from_sources(
            sources,
            "order_id",
            "orderId",
            "merchant_order_id",
            "merchantOrderId",
            "out_trade_no",
            "outTradeNo",
            "client_reference_id",
            "reference_id",
            "referenceId",
            "merchant_reference",
        ))
        if not order_id:
            raise PaymentWebhookPayloadError("order_id is required")

        event_id = _clean(_first_present(payload, "event_id", "eventId", "id")) or None
        provider_payment_id = _clean(_first_present_from_sources(
            [data, payload],
            "provider_payment_id",
            "payment_id",
            "paymentId",
            "transaction_id",
            "transactionId",
            "trade_no",
            "charge_id",
            "payment_intent",
            "paymentIntent",
            "charge",
            "prepay_id",
            "transaction_id",
            "transactionId",
            "refund_id",
        )) or None
        if provider_payment_id is None and data is not payload:
            provider_payment_id = _clean(data.get("id")) or None
        if provider_payment_id is None:
            provider_payment_id = event_id

        idempotency_key = _clean(_first_present_from_sources(
            [data, payload],
            "idempotency_key",
            "idempotencyKey",
            "event_id",
            "eventId",
            "request_id",
            "requestId",
            "notify_id",
        ))
        if not idempotency_key and event_id:
            idempotency_key = f"{provider}:event:{event_id}"
        if not idempotency_key and provider_payment_id:
            idempotency_key = f"{provider}:payment:{provider_payment_id}"
        if not idempotency_key:
            idempotency_key = f"{provider}:order:{order_id}"

        amount_value = _first_present_from_sources(
            sources,
            "amount_cents",
            "amount_total",
            "total_amount_cents",
            "paid_amount_cents",
            "refund_amount_cents",
            "refund_fee",
            "refund",
            "total",
            "payer_total",
        )
        amount_cents = _parse_amount_cents(amount_value)
        if amount_cents is None:
            decimal_amount = _first_present_from_sources(
                sources,
                "amount",
                "total_amount",
                "paid_amount",
                "refund_amount",
            )
            amount_cents = _parse_amount_cents(decimal_amount)
        currency_value = _first_present_from_sources(sources, "currency", "currency_code", "payer_currency")
        currency = _currency(currency_value) if _is_present(currency_value) else None

        refund_event_hint = "refund" in event_type
        should_refund = (
            event_type in self.REFUND_EVENT_TYPES
            or status in self.REFUND_STATUSES
            or (refund_event_hint and status in self.SUCCESS_STATUSES)
        )
        should_mark_paid = (
            not should_refund
            and not refund_event_hint
            and (
                event_type in self.SUCCESS_EVENT_TYPES
                or status in self.SUCCESS_STATUSES
            )
        )
        event_metadata: dict[str, object] = dict(metadata) if isinstance(metadata, dict) else {}
        event_metadata.update({
            "webhook_provider": provider,
            "webhook_event_type": event_type,
            "webhook_event_id": event_id,
            "webhook_status": status,
            "webhook_action": "refund" if should_refund else ("mark_paid" if should_mark_paid else "ignore"),
        })

        return PaymentWebhookEvent(
            provider=provider,
            event_type=event_type,
            event_id=event_id,
            status=status,
            order_id=order_id,
            provider_payment_id=provider_payment_id,
            idempotency_key=idempotency_key,
            amount_cents=amount_cents,
            currency=currency,
            metadata=event_metadata,
            should_mark_paid=should_mark_paid,
            should_refund=should_refund,
        )

    @staticmethod
    def _normalize_provider(provider: str) -> str:
        normalized = _clean(provider).lower()
        normalized = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "-" for ch in normalized).strip("-_")
        if not normalized:
            raise PaymentWebhookPayloadError("payment provider is required")
        return PaymentWebhookService.PROVIDER_ALIASES.get(normalized, normalized)[:64]

    @staticmethod
    def _decode_payload(body: bytes) -> dict[str, Any]:
        if not body:
            raise PaymentWebhookPayloadError("payment webhook body is empty")
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception as exc:
            raise PaymentWebhookPayloadError("payment webhook body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise PaymentWebhookPayloadError("payment webhook payload must be an object")
        return payload

    @staticmethod
    def _event_data(payload: dict[str, Any]) -> dict[str, Any]:
        stripe_object = _nested_dict(payload, "data", "object")
        if stripe_object:
            return stripe_object
        for key in ("data", "resource", "object", "payload"):
            value = payload.get(key)
            if isinstance(value, dict):
                return value
        return payload

    @staticmethod
    def _signature_part(signature_header: str, name: str) -> str:
        for part in signature_header.replace(";", ",").split(","):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            if key.strip().lower() == name:
                return value.strip()
        return ""

    @classmethod
    def _signature_candidates(cls, signature_header: str) -> list[str]:
        candidates: list[str] = []
        for part in signature_header.replace(";", ",").split(","):
            text = part.strip()
            if not text:
                continue
            if "=" in text:
                key, value = text.split("=", 1)
                key = key.strip().lower()
                if key in {"v1", "sha256", "signature", "sig"}:
                    text = value.strip()
                else:
                    continue
            elif text.lower().startswith("sha256:"):
                text = text.split(":", 1)[1].strip()
            if cls._looks_like_sha256_hex(text):
                candidates.append(text)
        if not candidates and cls._looks_like_sha256_hex(signature_header.strip()):
            candidates.append(signature_header.strip())
        return candidates

    @staticmethod
    def _looks_like_sha256_hex(value: str) -> bool:
        text = value.strip().lower()
        return len(text) == 64 and all(ch in "0123456789abcdef" for ch in text)

    @staticmethod
    def _check_timestamp(timestamp: str) -> None:
        tolerance = _safe_non_negative_int(os.getenv("PAYMENT_WEBHOOK_TOLERANCE_SECONDS"), 300)
        if tolerance <= 0:
            return
        timestamp_int = _safe_int(timestamp, -1)
        if timestamp_int < 0:
            raise PaymentWebhookSignatureError("payment webhook timestamp is invalid")
        if abs(int(time.time()) - timestamp_int) > tolerance:
            raise PaymentWebhookSignatureError("payment webhook timestamp is outside tolerance")

    @staticmethod
    def _secret_for_provider(provider: str) -> str:
        provider_env = _provider_env_name(provider)
        if provider_env:
            value = os.getenv(provider_env)
            if value:
                return value
        return os.getenv("PAYMENT_WEBHOOK_SECRET", "")


payment_webhook_service = PaymentWebhookService(billing_service)
