from __future__ import annotations

import os
from datetime import UTC, datetime
from html import escape
from typing import Any

from services.billing_service import BillingService, billing_service


def _clean(value: object) -> str:
    return str(value or "").strip()


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _currency(value: object) -> str:
    normalized = _clean(value).upper() or "CNY"
    return "".join(ch for ch in normalized if ch.isalnum())[:8] or "CNY"


def _parse_datetime(value: object) -> datetime | None:
    raw = _clean(value)
    if not raw:
        return None
    try:
        candidate = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if candidate.tzinfo is None:
        candidate = candidate.replace(tzinfo=UTC)
    return candidate.astimezone(UTC)


def _money(cents: object, currency: object = "CNY") -> str:
    return f"{_currency(currency)} {(_safe_int(cents) / 100):.2f}"


class ReceiptService:
    """Generate commercial receipt and refund-credit-note artifacts from orders."""

    def __init__(self, billing: BillingService):
        self.billing = billing

    @staticmethod
    def seller_profile() -> dict[str, object]:
        return {
            "name": _clean(os.getenv("BUSINESS_LEGAL_NAME")) or "ChatGPT2API",
            "tax_id": _clean(os.getenv("BUSINESS_TAX_ID")) or None,
            "address": _clean(os.getenv("BUSINESS_ADDRESS")) or None,
            "support_email": _clean(os.getenv("BUSINESS_SUPPORT_EMAIL")) or _clean(os.getenv("SUPPORT_EMAIL")) or None,
            "website": _clean(os.getenv("APP_PUBLIC_URL")) or _clean(os.getenv("CHATGPT2API_BASE_URL")) or None,
        }

    def build_order_receipt(self, order_id: str, identity: dict[str, object] | None = None) -> dict[str, object]:
        order = self.billing.get_order(order_id, identity)
        if order is None:
            raise ValueError("order not found")
        order_status = _clean(order.get("status"))
        if order_status not in {"paid", "fulfilled", "refunded"}:
            raise ValueError("receipt is available after payment")

        is_refund = order_status == "refunded"
        payment = self._payment_for_order(str(order.get("id") or ""), status="refunded" if is_refund else "succeeded")
        if payment is None:
            payment = self._payment_for_order(str(order.get("id") or ""))

        issued_at = _clean(
            order.get("refunded_at") if is_refund else order.get("fulfilled_at") or order.get("paid_at") or (payment or {}).get("paid_at")
        )
        if not issued_at:
            issued_at = datetime.now(UTC).isoformat()
        paid_at = _clean(order.get("paid_at") or (payment or {}).get("paid_at")) or None
        refunded_at = _clean(order.get("refunded_at") or (payment or {}).get("refunded_at")) or None
        receipt_number = self._receipt_number(order, issued_at, receipt_type="refund" if is_refund else "receipt")

        amount_cents = _safe_int(order.get("amount_cents"))
        signed_amount_cents = -amount_cents if is_refund else amount_cents
        quantity = max(1, _safe_int(order.get("quantity"), 1))
        quota_granted = _safe_int(order.get("quota_granted"))
        line = {
            "description": (
                f"Refund - {_clean(order.get('package_name') or order.get('package_id')) or 'Package'}"
                if is_refund
                else _clean(order.get("package_name") or order.get("package_id")) or "Package"
            ),
            "package_id": order.get("package_id"),
            "quantity": quantity,
            "quota": -quota_granted if is_refund else _safe_int(order.get("quota_total")),
            "unit_amount_cents": signed_amount_cents // quantity,
            "amount_cents": signed_amount_cents,
            "currency": _currency(order.get("currency")),
        }
        buyer = {
            "user_id": order.get("user_id"),
            "email": order.get("email"),
        }
        return {
            "id": f"{'rfnd' if is_refund else 'rcpt'}_{order.get('id')}",
            "receipt_type": "refund" if is_refund else "receipt",
            "receipt_number": receipt_number,
            "order_id": order.get("id"),
            "payment_id": (payment or {}).get("id"),
            "provider": (payment or {}).get("provider") or order.get("provider"),
            "provider_payment_id": (payment or {}).get("provider_payment_id") or order.get("provider_payment_id"),
            "status": "refunded" if is_refund else "issued",
            "seller": self.seller_profile(),
            "buyer": buyer,
            "currency": _currency(order.get("currency")),
            "amount_cents": signed_amount_cents,
            "amount_display": _money(signed_amount_cents, order.get("currency")),
            "tax_cents": 0,
            "total_cents": signed_amount_cents,
            "total_display": _money(signed_amount_cents, order.get("currency")),
            "lines": [line],
            "quota_granted": quota_granted,
            "quota_deducted": quota_granted if is_refund else 0,
            "package_expires_at": order.get("package_expires_at"),
            "paid_at": paid_at,
            "refunded_at": refunded_at,
            "issued_at": issued_at,
            "created_at": order.get("created_at"),
            "metadata": {
                "order_status": order.get("status"),
                "package_name": order.get("package_name"),
                "refund_reason": (order.get("metadata") or {}).get("refund_reason") if isinstance(order.get("metadata"), dict) else None,
            },
        }

    def _payment_for_order(self, order_id: str, status: str | None = None) -> dict[str, object] | None:
        normalized_status = _clean(status)
        payments = self.billing.list_payments(limit=1000)
        fallback: dict[str, object] | None = None
        for payment in payments:
            if payment.get("order_id") != order_id:
                continue
            if normalized_status and payment.get("status") != normalized_status:
                if fallback is None:
                    fallback = payment
                continue
            return payment
        if normalized_status:
            return fallback
        return None

    @staticmethod
    def _receipt_number(order: dict[str, object], issued_at: str, *, receipt_type: str = "receipt") -> str:
        issued = _parse_datetime(issued_at)
        date_part = issued.strftime("%Y%m%d") if issued else datetime.now(UTC).strftime("%Y%m%d")
        suffix = _clean(order.get("id")).replace("ord_", "")[-8:].upper() or "ORDER"
        prefix = "RFND" if receipt_type == "refund" else "RCPT"
        return f"{prefix}-{date_part}-{suffix}"

    def render_html(self, receipt: dict[str, object]) -> str:
        seller = receipt.get("seller") if isinstance(receipt.get("seller"), dict) else {}
        buyer = receipt.get("buyer") if isinstance(receipt.get("buyer"), dict) else {}
        lines = receipt.get("lines") if isinstance(receipt.get("lines"), list) else []
        line_rows = "\n".join(self._line_html(line) for line in lines if isinstance(line, dict))
        return f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>{escape(str(receipt.get("receipt_number") or "Receipt"))}</title>
    <style>
      body {{ margin: 0; background: #f5f5f4; color: #1c1917; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
      .page {{ max-width: 760px; margin: 32px auto; background: white; border: 1px solid #e7e5e4; border-radius: 24px; padding: 32px; }}
      h1 {{ margin: 0; font-size: 28px; }}
      .muted {{ color: #78716c; }}
      .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-top: 28px; }}
      .box {{ border: 1px solid #e7e5e4; border-radius: 16px; padding: 16px; }}
      table {{ width: 100%; border-collapse: collapse; margin-top: 28px; }}
      th, td {{ border-bottom: 1px solid #e7e5e4; padding: 12px; text-align: left; font-size: 14px; }}
      th {{ color: #78716c; font-size: 12px; text-transform: uppercase; letter-spacing: .08em; }}
      .right {{ text-align: right; }}
      .total {{ margin-top: 20px; text-align: right; font-size: 22px; font-weight: 700; }}
      @media print {{ body {{ background: white; }} .page {{ margin: 0; border: none; border-radius: 0; }} }}
    </style>
  </head>
  <body>
    <main class="page">
      <h1>Receipt / 收据</h1>
      <p class="muted">{escape(str(receipt.get("receipt_number") or ""))}</p>
      <div class="grid">
        <div class="box">
          <strong>Seller / 销售方</strong><br />
          {escape(str(seller.get("name") or ""))}<br />
          <span class="muted">{escape(str(seller.get("tax_id") or ""))}</span><br />
          <span class="muted">{escape(str(seller.get("support_email") or ""))}</span>
        </div>
        <div class="box">
          <strong>Buyer / 购买方</strong><br />
          {escape(str(buyer.get("email") or ""))}<br />
          <span class="muted">{escape(str(buyer.get("user_id") or ""))}</span><br />
          <span class="muted">Paid at: {escape(str(receipt.get("paid_at") or ""))}</span>
        </div>
      </div>
      <table>
        <thead><tr><th>Description</th><th>Qty</th><th>Quota</th><th class="right">Amount</th></tr></thead>
        <tbody>{line_rows}</tbody>
      </table>
      <div class="total">Total: {escape(str(receipt.get("total_display") or ""))}</div>
      <p class="muted">Order: {escape(str(receipt.get("order_id") or ""))} · Payment: {escape(str(receipt.get("payment_id") or ""))}</p>
    </main>
  </body>
</html>"""

    @staticmethod
    def _line_html(line: dict[str, object]) -> str:
        return (
            "<tr>"
            f"<td>{escape(str(line.get('description') or ''))}</td>"
            f"<td>{escape(str(line.get('quantity') or 0))}</td>"
            f"<td>{escape(str(line.get('quota') or 0))}</td>"
            f"<td class=\"right\">{escape(_money(line.get('amount_cents'), line.get('currency')))}</td>"
            "</tr>"
        )


receipt_service = ReceiptService(billing_service)
