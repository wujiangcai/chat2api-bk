from __future__ import annotations

import json
import unittest
from urllib.parse import urlsplit

from services.storage.migrations.versions import ALL_MIGRATIONS
from scripts.verify_production_deployment import REQUIRED_DEDICATED_COLLECTIONS, REQUIRED_READINESS_ITEM_IDS, HttpResult, upload_launch_evidence, verify_deployment


SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'self'",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}


def _json(status_code: int, body: dict[str, object], headers: dict[str, str] | None = None) -> HttpResult:
    return HttpResult(status_code=status_code, body=body, text="", headers=headers or {})


def _text(status_code: int, text: str) -> HttpResult:
    return HttpResult(status_code=status_code, body=None, text=text)


class VerifyProductionDeploymentTests(unittest.TestCase):
    @staticmethod
    def readiness_payload(status: str = "passed", ready: bool = True) -> dict[str, object]:
        return {
            "status": status,
            "ready": ready,
            "summary": {"failed": 0 if ready else 1},
            "items": [
                {"id": check_id, "status": "passed", "message": f"{check_id} passed"}
                for check_id in sorted(REQUIRED_READINESS_ITEM_IDS)
            ],
        }

    def production_fetcher(self, method: str, url: str, headers: dict[str, str], body, timeout: float) -> HttpResult:
        parsed = urlsplit(url)
        path = parsed.path
        query = f"?{parsed.query}" if parsed.query else ""
        route = f"{method} {path}{query}"
        self.assertIn("Authorization", headers) if path.startswith("/api/admin") or path.startswith("/api/storage") else None
        responses = {
            "GET /health/live": _json(200, {"status": "ok", "version": "test"}, headers=SECURITY_HEADERS),
            "GET /health/ready": _json(200, {"status": "healthy"}),
            "GET /auth/capabilities": _json(
                200,
                {
                    "registration_enabled": True,
                    "email_verification_required": True,
                    "session_cookie_enabled": True,
                    "password_reset_enabled": True,
                    "email_delivery_configured": True,
                    "email_provider": "smtp",
                },
            ),
            "GET /api/admin/production-readiness": _json(200, self.readiness_payload()),
            "GET /api/storage/info": _json(
                200,
                {
                    "backend": {"type": "database", "db_type": "postgresql"},
                    "health": {
                        "status": "healthy",
                        "schema_migration_count": len(ALL_MIGRATIONS),
                        "quota_ledger_count": 0,
                        "dedicated_collection_counts": {name: 0 for name in REQUIRED_DEDICATED_COLLECTIONS},
                    },
                    "object_storage": {"backend": "r2", "public_base_url": "https://cdn.example.com"},
                    "image_job_queue": {"backend": "redis", "queued_count": 0, "dead_letter_count": 0},
                    "rate_limit": {
                        "public_actions": {"backend": "redis", "healthy": True},
                        "api_keys": {"backend": "redis", "healthy": True},
                    },
                },
            ),
            "GET /api/admin/metrics?format=prometheus": _text(200, "chatgpt2api_up 1\n"),
            "GET /api/admin/alerts": _json(200, {"status": "healthy", "alerts": []}),
            "GET /api/admin/assets?limit=1": _json(200, {"items": []}),
        }
        return responses.get(route, HttpResult(status_code=404, body={"error": route}))

    def test_passes_required_remote_production_checks(self):
        result = verify_deployment(
            base_url="https://img.example.com",
            admin_key="admin-key",
            fetch=self.production_fetcher,
        )

        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["ready"])
        self.assertEqual(result["summary"]["failed"], 0)
        self.assertTrue(result["evidence"]["postgresql"])
        self.assertTrue(result["evidence"]["redis_queue"])
        self.assertTrue(result["evidence"]["redis_rate_limit"])
        self.assertTrue(result["evidence"]["remote_object_storage"])
        self.assertFalse(result["evidence"]["launch_evidence_strict_ready"])

    def test_fails_when_core_infrastructure_is_not_production_grade(self):
        def fetch(method: str, url: str, headers: dict[str, str], body, timeout: float) -> HttpResult:
            parsed = urlsplit(url)
            path = parsed.path
            query = f"?{parsed.query}" if parsed.query else ""
            route = f"{method} {path}{query}"
            if route == "GET /health/live":
                return _json(200, {"status": "ok"}, headers=SECURITY_HEADERS)
            if route == "GET /health/ready":
                return _json(200, {"status": "degraded"})
            if route == "GET /api/admin/production-readiness":
                return _json(200, {"status": "failed", "ready": False, "summary": {"failed": 3}, "items": [{"id": "storage.postgres", "status": "failed"}]})
            if route == "GET /auth/capabilities":
                return _json(
                    200,
                    {
                        "registration_enabled": True,
                        "email_verification_required": False,
                        "session_cookie_enabled": False,
                        "password_reset_enabled": True,
                        "email_delivery_configured": False,
                        "email_provider": "console",
                    },
                )
            if route == "GET /api/storage/info":
                return _json(
                    200,
                    {
                        "backend": {"type": "json"},
                        "health": {"status": "healthy"},
                        "object_storage": {"backend": "local"},
                        "image_job_queue": {"backend": "storage-polling"},
                        "rate_limit": {
                            "public_actions": {"backend": "memory"},
                            "api_keys": {"backend": "memory"},
                        },
                    },
                )
            if route == "GET /api/admin/metrics?format=prometheus":
                return _text(200, "chatgpt2api_up 1\n")
            if route == "GET /api/admin/alerts":
                return _json(200, {"alerts": [{"severity": "critical", "code": "storage_unhealthy"}]})
            if route == "GET /api/admin/assets?limit=1":
                return _json(200, {"items": []})
            return HttpResult(status_code=404)

        result = verify_deployment(
            base_url="https://img.example.com",
            admin_key="admin-key",
            fetch=fetch,
        )
        failed_ids = {item["id"] for item in result["checks"] if item["status"] == "failed"}

        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["ready"])
        self.assertIn("admin.production_readiness", failed_ids)
        self.assertIn("admin.production_readiness.required_items", failed_ids)
        self.assertIn("auth.capabilities.public_accounts", failed_ids)
        self.assertIn("storage.postgresql", failed_ids)
        self.assertIn("object_storage.remote", failed_ids)
        self.assertIn("queue.redis", failed_ids)
        self.assertIn("rate_limit.redis_runtime", failed_ids)
        self.assertIn("admin.alerts.no_critical", failed_ids)

    def test_optional_image_job_smoke_test_records_public_asset_url(self):
        poll_count = {"count": 0}

        def fetch(method: str, url: str, headers: dict[str, str], body, timeout: float) -> HttpResult:
            parsed = urlsplit(url)
            if parsed.netloc == "cdn.example.com":
                return _text(200, "image-bytes")
            path = parsed.path
            query = f"?{parsed.query}" if parsed.query else ""
            route = f"{method} {path}{query}"
            base = self.production_fetcher(method, url, headers, body, timeout)
            if base.status_code != 404:
                return base
            if route == "POST /api/jobs/images/generations":
                self.assertEqual(body["response_format"], "url")
                return _json(202, {"job": {"id": "job_1", "status": "queued"}})
            if route == "GET /api/jobs/job_1":
                poll_count["count"] += 1
                return _json(
                    200,
                    {
                        "job": {
                            "id": "job_1",
                            "status": "succeeded",
                            "assets": [{"url": "https://cdn.example.com/assets/job_1.png"}],
                        }
                    },
                )
            return HttpResult(status_code=404, body={"error": route})

        result = verify_deployment(
            base_url="https://img.example.com",
            admin_key="admin-key",
            fetch=fetch,
            run_image_job=True,
            poll_seconds=1,
            poll_interval=0.01,
        )
        check_ids = {item["id"]: item["status"] for item in result["checks"]}

        self.assertEqual(result["status"], "passed")
        self.assertEqual(check_ids["image_job.enqueue"], "passed")
        self.assertEqual(check_ids["image_job.succeeded"], "passed")
        self.assertEqual(check_ids["image_job.asset_url_public"], "passed")
        self.assertTrue(result["evidence"]["launch_evidence_strict_ready"])
        self.assertEqual(poll_count["count"], 1)

    def test_optional_payment_webhook_replay_records_paid_and_refund_evidence(self):
        seen: dict[str, object] = {"webhook_posts": 0}

        def fetch(method: str, url: str, headers: dict[str, str], body, timeout: float) -> HttpResult:
            parsed = urlsplit(url)
            path = parsed.path
            route = f"{method} {path}{'?' + parsed.query if parsed.query else ''}"
            base = self.production_fetcher(method, url, headers, body, timeout)
            if base.status_code != 404:
                return base
            if route == "POST /api/payments/webhook/stripe":
                seen["webhook_posts"] = int(seen["webhook_posts"]) + 1
                self.assertIn("Stripe-Signature", headers)
                payload = json.loads(body.decode("utf-8"))
                event_type = payload["type"]
                if event_type == "checkout.session.completed":
                    return _json(
                        200,
                        {
                            "ok": True,
                            "ignored": False,
                            "action": "mark_paid",
                            "order": {"id": "ord_verify", "status": "fulfilled"},
                            "payment": {"id": "pay_1", "status": "succeeded", "provider_payment_id": "pi_verify"},
                            "user": {"quota_balance": 50},
                        },
                    )
                if event_type == "refund.succeeded":
                    return _json(
                        200,
                        {
                            "ok": True,
                            "ignored": False,
                            "action": "refund",
                            "order": {"id": "ord_verify", "status": "refunded"},
                            "payment": {"id": "pay_1", "status": "refunded"},
                            "quota_deducted": 50,
                            "user": {"quota_balance": 0},
                        },
                    )
            return HttpResult(status_code=404, body={"error": route})

        result = verify_deployment(
            base_url="https://img.example.com",
            admin_key="admin-key",
            fetch=fetch,
            run_payment_webhook_replay=True,
            payment_webhook_provider="stripe",
            payment_webhook_secret="whsec_verify",
            payment_webhook_order_id="ord_verify",
        )
        check_ids = {item["id"]: item["status"] for item in result["checks"]}

        self.assertEqual(result["status"], "passed")
        self.assertEqual(check_ids["payment_webhook.replay.configured"], "passed")
        self.assertEqual(check_ids["payment_webhook.replay.paid"], "passed")
        self.assertEqual(check_ids["payment_webhook.replay.refund"], "passed")
        self.assertTrue(result["evidence"]["payment_webhook_replay_requested"])
        self.assertTrue(result["evidence"]["payment_webhook_paid_replay"])
        self.assertTrue(result["evidence"]["payment_webhook_refund_replay"])
        self.assertEqual(seen["webhook_posts"], 2)

    def test_payment_webhook_replay_requires_secret_and_order_id(self):
        result = verify_deployment(
            base_url="https://img.example.com",
            admin_key="admin-key",
            fetch=self.production_fetcher,
            run_payment_webhook_replay=True,
            payment_webhook_provider="stripe",
        )
        failed_ids = {item["id"] for item in result["checks"] if item["status"] == "failed"}

        self.assertEqual(result["status"], "failed")
        self.assertIn("payment_webhook.replay.configured", failed_ids)
        self.assertFalse(result["evidence"]["payment_webhook_paid_replay"])

    def test_optional_checkout_initiation_creates_disposable_order_and_checkout_evidence(self):
        seen: dict[str, object] = {"user_token_used": False, "cleanup_package": False, "cleanup_user": False}

        def fetch(method: str, url: str, headers: dict[str, str], body, timeout: float) -> HttpResult:
            parsed = urlsplit(url)
            route = f"{method} {parsed.path}{'?' + parsed.query if parsed.query else ''}"
            base = self.production_fetcher(method, url, headers, body, timeout)
            if base.status_code != 404:
                return base
            if route == "POST /api/admin/packages":
                self.assertEqual(body["price_cents"], 1990)
                self.assertEqual(body["quota"], 1)
                return _json(200, {"item": {"id": "pkg_checkout", "name": body["name"], "price_cents": 1990, "quota": 1}})
            if route == "POST /api/admin/users":
                return _json(200, {"item": {"id": "usr_checkout", "email": body["email"], "enabled": True}, "token": "user-token"})
            if route == "POST /api/orders":
                self.assertEqual(headers.get("Authorization"), "Bearer user-token")
                seen["user_token_used"] = True
                self.assertEqual(body["package_id"], "pkg_checkout")
                return _json(
                    200,
                    {
                        "order": {
                            "id": "ord_checkout",
                            "status": "pending_payment",
                            "package_id": "pkg_checkout",
                            "amount_cents": 1990,
                            "currency": "CNY",
                        }
                    },
                )
            if route == "POST /api/orders/ord_checkout/checkout":
                self.assertEqual(headers.get("Authorization"), "Bearer user-token")
                self.assertEqual(body["provider"], "redirect")
                return _json(
                    200,
                    {
                        "checkout": {
                            "id": "chk_checkout",
                            "provider": "redirect",
                            "mode": "redirect",
                            "order_id": "ord_checkout",
                            "amount_cents": 1990,
                            "currency": "CNY",
                            "payment_url": "https://pay.example.com/checkout?order=ord_checkout",
                            "status": "created",
                        },
                        "order": {
                            "id": "ord_checkout",
                            "status": "pending_payment",
                            "metadata": {"checkout": {"id": "chk_checkout"}},
                        },
                    },
                )
            if route == "POST /api/admin/packages/pkg_checkout":
                seen["cleanup_package"] = body == {"enabled": False}
                return _json(200, {"item": {"id": "pkg_checkout", "enabled": False}})
            if route == "POST /api/admin/users/usr_checkout":
                seen["cleanup_user"] = body == {"enabled": False}
                return _json(200, {"item": {"id": "usr_checkout", "enabled": False}})
            return HttpResult(status_code=404, body={"error": route})

        result = verify_deployment(
            base_url="https://img.example.com",
            admin_key="admin-key",
            fetch=fetch,
            run_checkout_initiation=True,
            checkout_provider="redirect",
        )
        check_ids = {item["id"]: item["status"] for item in result["checks"]}

        self.assertEqual(result["status"], "passed")
        self.assertEqual(check_ids["payment_checkout.fixtures"], "passed")
        self.assertEqual(check_ids["payment_checkout.order_created"], "passed")
        self.assertEqual(check_ids["payment_checkout.session_created"], "passed")
        self.assertEqual(check_ids["payment_checkout.cleanup.package_disabled"], "passed")
        self.assertEqual(check_ids["payment_checkout.cleanup.user_disabled"], "passed")
        self.assertTrue(result["evidence"]["payment_checkout_initiation_requested"])
        self.assertTrue(result["evidence"]["payment_checkout_order_created"])
        self.assertTrue(result["evidence"]["payment_checkout_session_created"])
        self.assertTrue(seen["user_token_used"])
        self.assertTrue(seen["cleanup_package"])
        self.assertTrue(seen["cleanup_user"])

    def test_checkout_webhook_replay_fulfills_and_refunds_same_disposable_order(self):
        seen: dict[str, object] = {"webhook_posts": 0, "cleanup_package": False, "cleanup_user": False}

        def fetch(method: str, url: str, headers: dict[str, str], body, timeout: float) -> HttpResult:
            parsed = urlsplit(url)
            route = f"{method} {parsed.path}{'?' + parsed.query if parsed.query else ''}"
            base = self.production_fetcher(method, url, headers, body, timeout)
            if base.status_code != 404:
                return base
            if route == "POST /api/admin/packages":
                return _json(200, {"item": {"id": "pkg_checkout", "name": body["name"], "price_cents": body["price_cents"], "quota": body["quota"]}})
            if route == "POST /api/admin/users":
                return _json(200, {"item": {"id": "usr_checkout", "email": body["email"], "enabled": True}, "token": "user-token"})
            if route == "POST /api/orders":
                self.assertEqual(headers.get("Authorization"), "Bearer user-token")
                return _json(
                    200,
                    {
                        "order": {
                            "id": "ord_checkout",
                            "status": "pending_payment",
                            "package_id": "pkg_checkout",
                            "amount_cents": 1990,
                            "currency": "CNY",
                        }
                    },
                )
            if route == "POST /api/orders/ord_checkout/checkout":
                self.assertEqual(headers.get("Authorization"), "Bearer user-token")
                return _json(
                    200,
                    {
                        "checkout": {
                            "id": "chk_checkout",
                            "provider": "redirect",
                            "mode": "redirect",
                            "order_id": "ord_checkout",
                            "amount_cents": 1990,
                            "currency": "CNY",
                            "payment_url": "https://pay.example.com/checkout?order=ord_checkout",
                            "status": "created",
                        },
                        "order": {"id": "ord_checkout", "status": "pending_payment", "metadata": {"checkout": {"id": "chk_checkout"}}},
                    },
                )
            if route == "POST /api/payments/webhook/stripe":
                seen["webhook_posts"] = int(seen["webhook_posts"]) + 1
                self.assertIn("Stripe-Signature", headers)
                payload = json.loads(body.decode("utf-8"))
                self.assertIn("ord_checkout", json.dumps(payload))
                if payload["type"] == "checkout.session.completed":
                    return _json(
                        200,
                        {
                            "ok": True,
                            "ignored": False,
                            "action": "mark_paid",
                            "order": {"id": "ord_checkout", "status": "fulfilled"},
                            "payment": {"id": "pay_1", "status": "succeeded", "provider_payment_id": "pi_checkout"},
                            "user": {"quota_balance": 1},
                        },
                    )
                if payload["type"] == "refund.succeeded":
                    return _json(
                        200,
                        {
                            "ok": True,
                            "ignored": False,
                            "action": "refund",
                            "order": {"id": "ord_checkout", "status": "refunded"},
                            "payment": {"id": "pay_1", "status": "refunded"},
                            "quota_deducted": 1,
                            "user": {"quota_balance": 0},
                        },
                    )
            if route == "POST /api/admin/packages/pkg_checkout":
                seen["cleanup_package"] = body == {"enabled": False}
                return _json(200, {"item": {"id": "pkg_checkout", "enabled": False}})
            if route == "POST /api/admin/users/usr_checkout":
                seen["cleanup_user"] = body == {"enabled": False}
                return _json(200, {"item": {"id": "usr_checkout", "enabled": False}})
            return HttpResult(status_code=404, body={"error": route})

        result = verify_deployment(
            base_url="https://img.example.com",
            admin_key="admin-key",
            fetch=fetch,
            run_checkout_webhook_replay=True,
            checkout_provider="redirect",
            checkout_webhook_provider="stripe",
            checkout_webhook_secret="whsec_checkout",
        )
        check_ids = {item["id"]: item["status"] for item in result["checks"]}

        self.assertEqual(result["status"], "passed")
        self.assertEqual(check_ids["payment_checkout.session_created"], "passed")
        self.assertEqual(check_ids["payment_checkout.webhook_replay.configured"], "passed")
        self.assertEqual(check_ids["payment_checkout.webhook_replay.paid"], "passed")
        self.assertEqual(check_ids["payment_checkout.webhook_replay.refund"], "passed")
        self.assertTrue(result["ran_checkout_initiation"])
        self.assertTrue(result["ran_checkout_webhook_replay"])
        self.assertTrue(result["evidence"]["payment_checkout_initiation_requested"])
        self.assertTrue(result["evidence"]["payment_checkout_webhook_replay_requested"])
        self.assertTrue(result["evidence"]["payment_checkout_paid_replay"])
        self.assertTrue(result["evidence"]["payment_checkout_refund_replay"])
        self.assertEqual(seen["webhook_posts"], 2)
        self.assertTrue(seen["cleanup_package"])
        self.assertTrue(seen["cleanup_user"])

    def test_strict_launch_requires_image_job_e2e_evidence(self):
        without_image_job = verify_deployment(
            base_url="https://img.example.com",
            admin_key="admin-key",
            fetch=self.production_fetcher,
            strict_launch=True,
        )
        failed_ids = {item["id"] for item in without_image_job["checks"] if item["status"] == "failed"}
        self.assertEqual(without_image_job["status"], "failed")
        self.assertIn("launch.strict_image_pipeline_e2e", failed_ids)

        def fetch(method: str, url: str, headers: dict[str, str], body, timeout: float) -> HttpResult:
            parsed = urlsplit(url)
            if parsed.netloc == "cdn.example.com":
                return _text(200, "image-bytes")
            route = f"{method} {parsed.path}{'?' + parsed.query if parsed.query else ''}"
            base = self.production_fetcher(method, url, headers, body, timeout)
            if base.status_code != 404:
                return base
            if route == "POST /api/jobs/images/generations":
                return _json(202, {"job": {"id": "job_1", "status": "queued"}})
            if route == "GET /api/jobs/job_1":
                return _json(
                    200,
                    {"job": {"id": "job_1", "status": "succeeded", "assets": [{"url": "https://cdn.example.com/assets/job_1.png"}]}},
                )
            return HttpResult(status_code=404)

        with_image_job = verify_deployment(
            base_url="https://img.example.com",
            admin_key="admin-key",
            fetch=fetch,
            run_image_job=True,
            strict_launch=True,
            poll_seconds=1,
            poll_interval=0.01,
        )
        self.assertEqual(with_image_job["status"], "passed")
        self.assertTrue(with_image_job["evidence"]["launch_evidence_strict_ready"])

    def test_upload_launch_evidence_posts_report_to_archive_api(self):
        captured: dict[str, object] = {}

        def fetch(method: str, url: str, headers: dict[str, str], body, timeout: float) -> HttpResult:
            captured["method"] = method
            captured["url"] = url
            captured["headers"] = headers
            captured["body"] = body
            return _json(200, {"item": {"id": "lev_1", "status": "passed"}})

        result = upload_launch_evidence(
            base_url="https://img.example.com",
            admin_key="admin-key",
            report={"status": "passed", "ready": True},
            name="prod launch",
            fetch=fetch,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["evidence_id"], "lev_1")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["url"], "https://img.example.com/api/admin/launch-evidence")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer admin-key")
        self.assertEqual(captured["body"]["name"], "prod launch")
        self.assertEqual(captured["body"]["source"], "remote-verifier")
        self.assertEqual(captured["body"]["report"]["status"], "passed")


if __name__ == "__main__":
    unittest.main()
