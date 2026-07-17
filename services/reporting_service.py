from __future__ import annotations

import os
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from services.config import config
from services.storage.base import StorageBackend


def _now() -> datetime:
    return datetime.now(UTC)


def _clean(value: object) -> str:
    return str(value or "").strip()


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_non_negative_int(value: object, default: int = 0) -> int:
    return max(0, _safe_int(value, default))


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


def _currency(value: object) -> str:
    normalized = _clean(value).upper() or "CNY"
    normalized = "".join(ch for ch in normalized if ch.isalnum())
    return normalized[:8] or "CNY"


def _counter_by(items: list[dict[str, Any]], key: str, *, default: str = "unknown") -> dict[str, int]:
    return dict(sorted(Counter(_clean(item.get(key)) or default for item in items).items()))


def _sum_amount_by_currency(items: list[dict[str, Any]], *, amount_key: str = "amount_cents") -> dict[str, int]:
    totals: dict[str, int] = {}
    for item in items:
        currency = _currency(item.get("currency"))
        totals[currency] = totals.get(currency, 0) + _safe_non_negative_int(item.get(amount_key))
    return dict(sorted(totals.items()))


def _subtract_currency_amount(totals: dict[str, int], currency: str, amount: int) -> dict[str, int]:
    result = dict(totals)
    result[currency] = result.get(currency, 0) - max(0, amount)
    return dict(sorted(result.items()))


def _is_dead_letter(job: dict[str, Any]) -> bool:
    error = job.get("error") if isinstance(job.get("error"), dict) else {}
    return bool(job.get("dead_lettered_at") or (isinstance(error, dict) and error.get("dead_lettered")))


def _job_cost_units(job: dict[str, Any]) -> int:
    explicit = _safe_non_negative_int(job.get("cost_units"))
    if explicit > 0:
        return explicit
    if job.get("status") != "succeeded":
        return 0
    assets = job.get("assets")
    if isinstance(assets, list) and assets:
        return len(assets)
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    data = result.get("data") if isinstance(result, dict) else None
    if isinstance(data, list) and data:
        return sum(1 for item in data if isinstance(item, dict) and not item.get("error"))
    request = job.get("request") if isinstance(job.get("request"), dict) else {}
    return max(1, _safe_int(request.get("n"), 1))


class ReportingService:
    """Business reporting for commercial operations.

    The report intentionally reads from the storage abstraction instead of a
    specific database session so the same API works with JSON, SQLite and
    PostgreSQL deployments. It is a point-in-time snapshot, not a cached
    warehouse, which keeps the first commercial reporting iteration simple and
    deterministic.
    """

    def __init__(
        self,
        storage_factory: Callable[[], StorageBackend],
        *,
        cost_per_image_cents_env: str = "COST_PER_IMAGE_CENTS",
        cost_currency_env: str = "COST_CURRENCY",
    ):
        self.storage_factory = storage_factory
        self.cost_per_image_cents_env = cost_per_image_cents_env
        self.cost_currency_env = cost_currency_env

    def collect(self, *, days: int = 30) -> dict[str, Any]:
        safe_days = min(max(1, _safe_int(days, 30)), 3660)
        generated_at = _now()
        window_start = generated_at - timedelta(days=safe_days)
        collections = self._load_collections()
        cost_per_image_cents = _safe_non_negative_int(os.getenv(self.cost_per_image_cents_env), 0)
        cost_currency = _currency(os.getenv(self.cost_currency_env) or "CNY")

        all_time = self._period_report(
            collections,
            label="all_time",
            start_at=None,
            cost_per_image_cents=cost_per_image_cents,
            cost_currency=cost_currency,
        )
        window = self._period_report(
            collections,
            label=f"last_{safe_days}_days",
            start_at=window_start,
            cost_per_image_cents=cost_per_image_cents,
            cost_currency=cost_currency,
        )

        return {
            "generated_at": generated_at.isoformat(),
            "window_days": safe_days,
            "window_start": window_start.isoformat(),
            "window_end": generated_at.isoformat(),
            "cost_per_image_cents": cost_per_image_cents,
            "cost_currency": cost_currency,
            "summary": {
                "users_total": all_time["users"]["total"],
                "users_enabled_total": all_time["users"]["enabled"],
                "quota_balance_total": all_time["quota"]["current_balance_total"],
                "gross_revenue_cents_by_currency": all_time["payments"]["gross_revenue_cents_by_currency"],
                "window_gross_revenue_cents_by_currency": window["payments"]["gross_revenue_cents_by_currency"],
                "estimated_image_cost_cents": all_time["unit_economics"]["estimated_image_cost_cents"],
                "window_estimated_image_cost_cents": window["unit_economics"]["estimated_image_cost_cents"],
                "estimated_gross_margin_cents_by_currency": all_time["unit_economics"]["estimated_gross_margin_cents_by_currency"],
                "window_estimated_gross_margin_cents_by_currency": window["unit_economics"]["estimated_gross_margin_cents_by_currency"],
                "orders_total": all_time["orders"]["total"],
                "orders_pending_total": all_time["orders"]["pending_total"],
                "orders_fulfilled_total": all_time["orders"]["fulfilled_total"],
                "payments_succeeded_total": all_time["payments"]["succeeded_total"],
                "image_jobs_success_rate": all_time["image_jobs"]["success_rate"],
                "window_image_jobs_success_rate": window["image_jobs"]["success_rate"],
                "image_jobs_dead_letter_total": all_time["image_jobs"]["dead_letter_total"],
                "image_assets_active_total": all_time["image_assets"]["active_total"],
                "image_assets_active_bytes": all_time["image_assets"]["active_size_bytes"],
            },
            "all_time": all_time,
            "window": window,
        }

    def _load_collections(self) -> dict[str, list[dict[str, Any]]]:
        storage = self.storage_factory()
        names = [
            "users",
            "packages",
            "orders",
            "payments",
            "quota_ledger",
            "image_jobs",
            "image_assets",
            "redemptions",
        ]
        collections: dict[str, list[dict[str, Any]]] = {}
        for name in names:
            try:
                items = storage.load_collection(name)
            except Exception:
                items = []
            collections[name] = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
        try:
            keys = storage.load_auth_keys()
        except Exception:
            keys = []
        collections["auth_keys"] = [item for item in keys if isinstance(item, dict)] if isinstance(keys, list) else []
        return collections

    @staticmethod
    def _filter_since(items: list[dict[str, Any]], start_at: datetime | None, *date_keys: str) -> list[dict[str, Any]]:
        if start_at is None:
            return list(items)
        filtered: list[dict[str, Any]] = []
        for item in items:
            timestamp = None
            for key in date_keys:
                timestamp = _parse_datetime(item.get(key))
                if timestamp is not None:
                    break
            if timestamp is not None and timestamp >= start_at:
                filtered.append(item)
        return filtered

    def _period_report(
        self,
        collections: dict[str, list[dict[str, Any]]],
        *,
        label: str,
        start_at: datetime | None,
        cost_per_image_cents: int,
        cost_currency: str,
    ) -> dict[str, Any]:
        users = self._filter_since(collections["users"], start_at, "created_at")
        packages = self._filter_since(collections["packages"], start_at, "created_at")
        orders = self._filter_since(collections["orders"], start_at, "created_at")
        payments = self._filter_since(collections["payments"], start_at, "paid_at", "created_at")
        quota_ledger = self._filter_since(collections["quota_ledger"], start_at, "created_at")
        image_jobs = self._filter_since(collections["image_jobs"], start_at, "created_at")
        image_assets = self._filter_since(collections["image_assets"], start_at, "created_at")
        redemptions = self._filter_since(collections["redemptions"], start_at, "redeemed_at", "created_at")

        users_report = self._users_report(users, collections["users"])
        orders_report = self._orders_report(orders)
        payments_report = self._payments_report(payments)
        quota_report = self._quota_report(quota_ledger, collections["users"])
        jobs_report = self._image_jobs_report(image_jobs)
        assets_report = self._image_assets_report(image_assets)
        estimated_image_cost_cents = jobs_report["cost_units"] * cost_per_image_cents

        return {
            "label": label,
            "start_at": start_at.isoformat() if start_at else None,
            "users": users_report,
            "auth_keys": {
                "total": len(collections["auth_keys"]) if start_at is None else None,
                "enabled_total": sum(1 for item in collections["auth_keys"] if bool(item.get("enabled", True))) if start_at is None else None,
            },
            "packages": {
                "total": len(packages),
                "enabled_total": sum(1 for item in packages if bool(item.get("enabled", True))),
                "disabled_total": sum(1 for item in packages if not bool(item.get("enabled", True))),
            },
            "orders": orders_report,
            "payments": payments_report,
            "quota": quota_report,
            "redemptions": {
                "total": len(redemptions),
                "quota_granted": sum(_safe_non_negative_int(item.get("quota_granted")) for item in redemptions),
                "by_type": _counter_by(redemptions, "type"),
            },
            "image_jobs": jobs_report,
            "image_assets": assets_report,
            "unit_economics": {
                "cost_per_image_cents": cost_per_image_cents,
                "cost_currency": cost_currency,
                "estimated_image_cost_cents": estimated_image_cost_cents,
                "gross_revenue_cents_by_currency": payments_report["gross_revenue_cents_by_currency"],
                "net_revenue_cents_by_currency": payments_report["net_revenue_cents_by_currency"],
                "estimated_gross_margin_cents_by_currency": _subtract_currency_amount(
                    dict(payments_report["net_revenue_cents_by_currency"]),
                    cost_currency,
                    estimated_image_cost_cents,
                ),
            },
        }

    @staticmethod
    def _users_report(users: list[dict[str, Any]], all_users: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "total": len(users),
            "enabled": sum(1 for item in users if bool(item.get("enabled", True))),
            "disabled": sum(1 for item in users if not bool(item.get("enabled", True))),
            "with_positive_quota": sum(1 for item in users if _safe_int(item.get("quota_balance")) > 0),
            "quota_balance_total": sum(_safe_non_negative_int(item.get("quota_balance")) for item in users),
            "current_total": len(all_users),
            "current_enabled": sum(1 for item in all_users if bool(item.get("enabled", True))),
        }

    @staticmethod
    def _orders_report(orders: list[dict[str, Any]]) -> dict[str, Any]:
        by_status = _counter_by(orders, "status")
        pending_statuses = {"created", "pending_payment"}
        paid_statuses = {"paid", "fulfilled"}
        return {
            "total": len(orders),
            "by_status": by_status,
            "pending_total": sum(by_status.get(status, 0) for status in pending_statuses),
            "paid_total": by_status.get("paid", 0),
            "fulfilled_total": by_status.get("fulfilled", 0),
            "cancelled_total": by_status.get("cancelled", 0),
            "refunded_total": by_status.get("refunded", 0),
            "payable_amount_cents_by_currency": _sum_amount_by_currency(
                [item for item in orders if _clean(item.get("status")) in pending_statuses],
            ),
            "paid_or_fulfilled_amount_cents_by_currency": _sum_amount_by_currency(
                [item for item in orders if _clean(item.get("status")) in paid_statuses],
            ),
            "order_amount_cents_by_currency": _sum_amount_by_currency(orders),
            "quota_total": sum(_safe_non_negative_int(item.get("quota_total")) for item in orders),
            "quota_granted": sum(_safe_non_negative_int(item.get("quota_granted")) for item in orders),
        }

    @staticmethod
    def _payments_report(payments: list[dict[str, Any]]) -> dict[str, Any]:
        by_status = _counter_by(payments, "status")
        succeeded = [item for item in payments if _clean(item.get("status")) == "succeeded"]
        refunded = [item for item in payments if _clean(item.get("status")) == "refunded"]
        gross = _sum_amount_by_currency([*succeeded, *refunded])
        refund_totals = _sum_amount_by_currency(refunded)
        net = dict(gross)
        for currency, amount in refund_totals.items():
            net[currency] = net.get(currency, 0) - amount
        return {
            "total": len(payments),
            "by_status": by_status,
            "succeeded_total": len(succeeded),
            "refunded_total": len(refunded),
            "gross_revenue_cents_by_currency": dict(sorted(gross.items())),
            "refunded_cents_by_currency": dict(sorted(refund_totals.items())),
            "net_revenue_cents_by_currency": dict(sorted(net.items())),
            "by_provider": _counter_by(payments, "provider"),
        }

    @staticmethod
    def _quota_report(ledger: list[dict[str, Any]], all_users: list[dict[str, Any]]) -> dict[str, Any]:
        by_type = _counter_by(ledger, "type")
        positive = sum(max(0, _safe_int(item.get("amount"))) for item in ledger)
        negative = sum(abs(min(0, _safe_int(item.get("amount")))) for item in ledger)
        return {
            "ledger_total": len(ledger),
            "by_type": by_type,
            "granted_units": sum(max(0, _safe_int(item.get("amount"))) for item in ledger if _clean(item.get("type")) == "grant"),
            "consumed_units": sum(abs(_safe_int(item.get("amount"))) for item in ledger if _clean(item.get("type")) == "consume"),
            "refunded_units": sum(abs(_safe_int(item.get("amount"))) for item in ledger if _clean(item.get("type")) == "refund"),
            "adjusted_units": sum(_safe_int(item.get("amount")) for item in ledger if _clean(item.get("type")) in {"adjust", "set"}),
            "positive_units": positive,
            "negative_units": negative,
            "net_units": sum(_safe_int(item.get("amount")) for item in ledger),
            "current_balance_total": sum(_safe_non_negative_int(item.get("quota_balance")) for item in all_users),
        }

    @staticmethod
    def _image_jobs_report(jobs: list[dict[str, Any]]) -> dict[str, Any]:
        by_status = _counter_by(jobs, "status")
        succeeded = by_status.get("succeeded", 0)
        failed = by_status.get("failed", 0)
        attempted = succeeded + failed
        cost_units = sum(_job_cost_units(job) for job in jobs)
        success_rate = round(succeeded / attempted, 4) if attempted else None
        failure_rate = round(failed / attempted, 4) if attempted else None
        return {
            "total": len(jobs),
            "by_status": by_status,
            "queued_total": by_status.get("queued", 0),
            "running_total": by_status.get("running", 0),
            "succeeded_total": succeeded,
            "failed_total": failed,
            "cancelled_total": by_status.get("cancelled", 0),
            "dead_letter_total": sum(1 for job in jobs if _is_dead_letter(job)),
            "success_rate": success_rate,
            "failure_rate": failure_rate,
            "reserved_quota": sum(_safe_non_negative_int(job.get("reserved_quota")) for job in jobs),
            "refunded_quota": sum(_safe_non_negative_int(job.get("refunded_quota")) for job in jobs),
            "cost_units": cost_units,
            "attempts_total": sum(_safe_non_negative_int(job.get("attempts")) for job in jobs),
        }

    @staticmethod
    def _image_assets_report(assets: list[dict[str, Any]]) -> dict[str, Any]:
        active = [item for item in assets if _clean(item.get("status")) != "deleted"]
        deleted = [item for item in assets if _clean(item.get("status")) == "deleted"]
        return {
            "total": len(assets),
            "active_total": len(active),
            "deleted_total": len(deleted),
            "by_status": _counter_by(assets, "status"),
            "size_bytes": sum(_safe_non_negative_int(item.get("size_bytes")) for item in assets),
            "active_size_bytes": sum(_safe_non_negative_int(item.get("size_bytes")) for item in active),
            "deleted_size_bytes": sum(_safe_non_negative_int(item.get("size_bytes")) for item in deleted),
        }


reporting_service = ReportingService(config.get_storage_backend)
