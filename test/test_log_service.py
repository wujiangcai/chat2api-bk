from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from services.log_service import LOG_TYPE_CALL, LogService, REDACTED, sanitize_log_value


class LogServiceTests(unittest.TestCase):
    def create_log_service(self) -> tuple[tempfile.TemporaryDirectory[str], LogService]:
        tmp_dir = tempfile.TemporaryDirectory()
        return tmp_dir, LogService(Path(tmp_dir.name) / "logs.jsonl")

    def test_add_redacts_sensitive_fields_but_keeps_safe_ids(self):
        tmp_dir, service = self.create_log_service()
        self.addCleanup(tmp_dir.cleanup)

        service.add(
            LOG_TYPE_CALL,
            "call completed",
            {
                "authorization": "Bearer sk-secret",
                "api_key": "c2a-secret",
                "key_id": "key_123",
                "provider_payment_id": "pay_123",
                "idempotency_key": "idem_123",
                "code_prefix": "ABCD",
            },
        )

        detail = service.list()[0]["detail"]
        self.assertEqual(detail["authorization"], REDACTED)
        self.assertEqual(detail["api_key"], REDACTED)
        self.assertEqual(detail["key_id"], "key_123")
        self.assertEqual(detail["provider_payment_id"], "pay_123")
        self.assertEqual(detail["idempotency_key"], "idem_123")
        self.assertEqual(detail["code_prefix"], "ABCD")

    def test_nested_values_and_image_payloads_are_sanitized(self):
        value = sanitize_log_value(
            {
                "nested": {
                    "refresh_token": "refresh-secret",
                    "items": [
                        {"password": "password-secret"},
                        {"b64_json": "a" * 4096},
                        {"url": "data:image/png;base64," + "b" * 4096},
                    ],
                },
                "input_image": "raw-image-bytes",
                "safe": "x" * 3000,
            }
        )

        self.assertEqual(value["nested"]["refresh_token"], REDACTED)
        self.assertEqual(value["nested"]["items"][0]["password"], REDACTED)
        self.assertEqual(value["nested"]["items"][1]["b64_json"], REDACTED)
        self.assertEqual(value["nested"]["items"][2]["url"], "data:image/png;base64,[REDACTED]")
        self.assertEqual(value["input_image"], REDACTED)
        self.assertTrue(value["safe"].endswith("...[TRUNCATED]"))
        self.assertLess(len(value["safe"]), 2100)

    def test_url_query_tokens_are_redacted(self):
        value = sanitize_log_value(
            {
                "url": (
                    "https://cdn.example.com/image.png?"
                    "token=token-secret&access_token=access-secret&key=key-secret&"
                    "idempotency_key=idem_123&provider_payment_id=pay_123&plain=ok"
                )
            }
        )

        query = parse_qs(urlsplit(value["url"]).query)
        self.assertEqual(query["token"], [REDACTED])
        self.assertEqual(query["access_token"], [REDACTED])
        self.assertEqual(query["key"], [REDACTED])
        self.assertEqual(query["idempotency_key"], ["idem_123"])
        self.assertEqual(query["provider_payment_id"], ["pay_123"])
        self.assertEqual(query["plain"], ["ok"])
        self.assertNotIn("token-secret", value["url"])
        self.assertNotIn("access-secret", value["url"])
        self.assertNotIn("key-secret", value["url"])

    def test_list_sanitizes_legacy_raw_log_lines(self):
        tmp_dir, service = self.create_log_service()
        self.addCleanup(tmp_dir.cleanup)
        service.path.write_text(
            json.dumps(
                {
                    "time": "2026-07-07 10:00:00",
                    "type": LOG_TYPE_CALL,
                    "summary": "legacy",
                    "detail": {
                        "password": "legacy-secret",
                        "urls": ["https://cdn.example.com/a.png?token=legacy-token&plain=ok"],
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        item = service.list()[0]
        self.assertEqual(item["detail"]["password"], REDACTED)
        self.assertNotIn("legacy-token", item["detail"]["urls"][0])
        self.assertIn("plain=ok", item["detail"]["urls"][0])


if __name__ == "__main__":
    unittest.main()
