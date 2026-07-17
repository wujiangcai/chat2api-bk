from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import shlex
import sys
import time
import urllib.error
import urllib.request
from typing import Any


PROVIDER_ALIASES = {
    "wechat": "wechatpay",
    "wechat-pay": "wechatpay",
    "weixin": "wechatpay",
    "weixin-pay": "wechatpay",
}


def normalize_provider(provider: str) -> str:
    normalized = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "-" for ch in str(provider or "").strip().lower()).strip("-_")
    if not normalized:
        raise ValueError("provider is required")
    return PROVIDER_ALIASES.get(normalized, normalized)


def build_payload(
    *,
    provider: str,
    action: str,
    order_id: str,
    amount_cents: int = 1990,
    currency: str = "CNY",
    event_id: str = "",
    provider_payment_id: str = "",
) -> dict[str, Any]:
    """Build a local signed-webhook sample payload.

    These are intentionally HMAC-signed sandbox payloads that match this
    service's provider adapter fields. They are for local deployment acceptance
    and gateway-normalized webhooks, not a replacement for each provider's
    official SDK/certificate flow.
    """

    normalized_provider = normalize_provider(provider)
    normalized_action = str(action or "paid").strip().lower().replace("_", "-")
    amount_cents = max(0, int(amount_cents or 0))
    currency = "".join(ch for ch in str(currency or "CNY").upper() if ch.isalnum())[:8] or "CNY"
    event_id = event_id or f"evt_sandbox_{normalized_provider}_{normalized_action}_{int(time.time())}"
    provider_payment_id = provider_payment_id or f"pay_sandbox_{order_id}"

    if normalized_provider == "stripe":
        if normalized_action == "refund":
            return {
                "id": event_id,
                "type": "refund.succeeded",
                "data": {
                    "object": {
                        "id": f"re_sandbox_{order_id}",
                        "order_id": order_id,
                        "payment_intent": provider_payment_id,
                        "amount": amount_cents,
                        "currency": currency.lower(),
                        "metadata": {"order_id": order_id, "sandbox": "true"},
                    },
                },
            }
        if normalized_action == "pending-refund":
            return {
                "id": event_id,
                "type": "refund.created",
                "data": {
                    "object": {
                        "id": f"re_pending_{order_id}",
                        "order_id": order_id,
                        "status": "pending",
                        "amount": amount_cents,
                        "currency": currency.lower(),
                    },
                },
            }
        return {
            "id": event_id,
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": f"cs_sandbox_{order_id}",
                    "client_reference_id": order_id,
                    "payment_intent": provider_payment_id,
                    "amount_total": amount_cents,
                    "currency": currency.lower(),
                    "metadata": {"order_id": order_id, "sandbox": "true"},
                },
            },
        }

    if normalized_provider == "alipay":
        if normalized_action == "refund":
            return {
                "notify_id": event_id,
                "notify_type": "trade_refund_success",
                "out_trade_no": order_id,
                "trade_no": provider_payment_id,
                "trade_status": "REFUND_SUCCESS",
                "refund_fee": f"{amount_cents / 100:.2f}",
                "currency": currency,
            }
        if normalized_action == "pending-refund":
            return {
                "notify_id": event_id,
                "notify_type": "trade_refund_created",
                "out_trade_no": order_id,
                "trade_no": provider_payment_id,
                "trade_status": "REFUND_PROCESSING",
                "refund_fee": f"{amount_cents / 100:.2f}",
                "currency": currency,
            }
        return {
            "notify_id": event_id,
            "notify_type": "trade_success",
            "out_trade_no": order_id,
            "trade_no": provider_payment_id,
            "trade_status": "TRADE_SUCCESS",
            "total_amount": f"{amount_cents / 100:.2f}",
            "currency": currency,
        }

    if normalized_provider == "wechatpay":
        if normalized_action == "refund":
            return {
                "id": event_id,
                "event_type": "REFUND.SUCCESS",
                "resource": {
                    "out_trade_no": order_id,
                    "transaction_id": provider_payment_id,
                    "refund_id": f"refund_sandbox_{order_id}",
                    "refund_status": "SUCCESS",
                    "amount": {"refund": amount_cents, "currency": currency},
                },
            }
        if normalized_action == "pending-refund":
            return {
                "id": event_id,
                "event_type": "REFUND.PROCESSING",
                "resource": {
                    "out_trade_no": order_id,
                    "transaction_id": provider_payment_id,
                    "refund_status": "PROCESSING",
                    "amount": {"refund": amount_cents, "currency": currency},
                },
            }
        return {
            "id": event_id,
            "event_type": "TRANSACTION.SUCCESS",
            "resource": {
                "out_trade_no": order_id,
                "transaction_id": provider_payment_id,
                "trade_state": "SUCCESS",
                "amount": {"total": amount_cents, "currency": currency},
            },
        }

    event_type = "refund.succeeded" if normalized_action == "refund" else "payment.succeeded"
    status = "refunded" if normalized_action == "refund" else "succeeded"
    if normalized_action == "pending-refund":
        event_type = "refund.created"
        status = "pending"
    return {
        "id": event_id,
        "type": event_type,
        "order_id": order_id,
        "provider_payment_id": provider_payment_id,
        "status": status,
        "amount_cents": amount_cents,
        "currency": currency,
        "metadata": {"sandbox": "true"},
    }


def canonical_body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def sign_headers(
    *,
    provider: str,
    secret: str,
    body: bytes,
    timestamp: int | None = None,
) -> dict[str, str]:
    normalized_provider = normalize_provider(provider)
    timestamp = int(timestamp or time.time())
    signature = hmac.new(secret.encode("utf-8"), f"{timestamp}.".encode("utf-8") + body, hashlib.sha256).hexdigest()
    if normalized_provider == "stripe":
        return {
            "Content-Type": "application/json",
            "Stripe-Signature": f"t={timestamp},v1={signature}",
        }
    if normalized_provider == "alipay":
        return {
            "Content-Type": "application/json",
            "Alipay-Timestamp": str(timestamp),
            "Alipay-Signature": f"t={timestamp},v1={signature}",
        }
    if normalized_provider == "wechatpay":
        return {
            "Content-Type": "application/json",
            "Wechatpay-Timestamp": str(timestamp),
            "Wechatpay-Signature": f"t={timestamp},v1={signature}",
        }
    return {
        "Content-Type": "application/json",
        "X-Payment-Timestamp": str(timestamp),
        "X-Payment-Signature": f"t={timestamp},v1={signature}",
    }


def curl_command(url: str, headers: dict[str, str], body: bytes) -> str:
    parts = ["curl", "-i", "-X", "POST"]
    for key, value in headers.items():
        parts.extend(["-H", f"{key}: {value}"])
    parts.extend(["--data-binary", body.decode("utf-8"), url])
    return " ".join(shlex.quote(part) for part in parts)


def post(url: str, headers: dict[str, str], body: bytes, timeout: int = 15) -> tuple[int, str]:
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read().decode("utf-8", errors="replace")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate or replay a signed payment webhook sandbox payload.")
    parser.add_argument("--provider", default="stripe", help="stripe, alipay, wechatpay or generic")
    parser.add_argument("--action", default="paid", choices=["paid", "refund", "pending-refund"], help="sample event action")
    parser.add_argument("--order-id", required=True, help="commercial order id, e.g. ord_xxx")
    parser.add_argument("--amount-cents", type=int, default=1990)
    parser.add_argument("--currency", default="CNY")
    parser.add_argument("--provider-payment-id", default="")
    parser.add_argument("--event-id", default="")
    parser.add_argument("--secret", required=True, help="PAYMENT_WEBHOOK_SECRET or provider-specific secret")
    parser.add_argument("--base-url", default="", help="if set, prints/sends to <base-url>/api/payments/webhook/{provider}")
    parser.add_argument("--url", default="", help="full webhook URL; overrides --base-url")
    parser.add_argument("--send", action="store_true", help="POST the signed payload instead of only printing it")
    parser.add_argument("--timeout", type=int, default=15)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(argv if argv is not None else sys.argv[1:]))
    provider = normalize_provider(args.provider)
    payload = build_payload(
        provider=provider,
        action=args.action,
        order_id=args.order_id,
        amount_cents=args.amount_cents,
        currency=args.currency,
        event_id=args.event_id,
        provider_payment_id=args.provider_payment_id,
    )
    body = canonical_body(payload)
    headers = sign_headers(provider=provider, secret=args.secret, body=body)
    url = args.url or (args.base_url.rstrip("/") + f"/api/payments/webhook/{provider}" if args.base_url else "")
    print(json.dumps({"provider": provider, "payload": payload, "headers": headers}, ensure_ascii=False, indent=2))
    if url:
        print("\n# curl")
        print(curl_command(url, headers, body))
    if args.send:
        if not url:
            raise SystemExit("--send requires --url or --base-url")
        status, text = post(url, headers, body, timeout=args.timeout)
        print(f"\n# response {status}")
        print(text)
        return 0 if 200 <= status < 300 else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
