from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api import tickets
from services.object_storage import LocalObjectStorage
from services.storage.json_storage import JSONStorageBackend
from services.support_ticket_service import SupportTicketService


class FakeEmailService:
    def __init__(self):
        self.sent: list[dict[str, object]] = []

    def send_support_ticket_update(self, **kwargs):
        self.sent.append(kwargs)

        class Result:
            sent = True
            message = "sent"

        return Result()


class SupportTicketApiTests(unittest.TestCase):
    def create_context(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        root = Path(tmp_dir.name)
        storage = JSONStorageBackend(root / "accounts.json", root / "auth_keys.json")
        service = SupportTicketService(storage, object_storage=LocalObjectStorage(root / "assets"))
        app = FastAPI()
        app.include_router(tickets.create_router())
        return {"client": TestClient(app), "service": service}

    @staticmethod
    def fake_require_identity(authorization: str | None):
        if authorization == "Bearer user-token":
            return {"role": "user", "user_id": "usr_api", "email": "user@example.com", "name": "User"}
        if authorization == "Bearer other-token":
            return {"role": "user", "user_id": "usr_other", "email": "other@example.com", "name": "Other"}
        if authorization == "Bearer admin-token":
            return {"role": "admin", "id": "admin", "email": "admin@example.com", "name": "Admin", "permissions": ["*"]}
        raise HTTPException(status_code=401, detail={"error": "authorization is invalid"})

    @classmethod
    def fake_require_admin(cls, authorization: str | None):
        identity = cls.fake_require_identity(authorization)
        if identity.get("role") != "admin":
            raise HTTPException(status_code=403, detail={"error": "admin permission required"})
        return identity

    def patch_api(self, ctx):
        return patch.multiple(
            tickets,
            support_ticket_service=ctx["service"],
            require_identity=self.fake_require_identity,
            require_admin=self.fake_require_admin,
        )

    def test_user_creates_ticket_and_admin_resolves(self):
        ctx = self.create_context()
        client: TestClient = ctx["client"]
        fake_email = FakeEmailService()

        with self.patch_api(ctx), patch.dict("os.environ", {"SUPPORT_TICKET_EMAIL_NOTIFICATIONS_ENABLED": "true"}, clear=False), patch.object(tickets, "email_service", fake_email):
            create_response = client.post(
                "/api/support/tickets",
                headers={"Authorization": "Bearer user-token"},
                json={"subject": "Refund question", "message": "Can I get help?", "category": "refund", "priority": "normal"},
            )
            self.assertEqual(create_response.status_code, 200)
            ticket = create_response.json()["item"]

            admin_list = client.get("/api/admin/support/tickets", headers={"Authorization": "Bearer admin-token"})
            self.assertEqual(admin_list.status_code, 200)
            self.assertEqual(len(admin_list.json()["items"]), 1)

            reply_response = client.post(
                f"/api/admin/support/tickets/{ticket['id']}/messages",
                headers={"Authorization": "Bearer admin-token"},
                json={"message": "Refund policy link attached.", "internal": False},
            )
            self.assertEqual(reply_response.status_code, 200)
            self.assertEqual(reply_response.json()["item"]["status"], "in_progress")

            update_response = client.post(
                f"/api/admin/support/tickets/{ticket['id']}",
                headers={"Authorization": "Bearer admin-token"},
                json={"status": "resolved", "priority": "low", "tags": ["refund", "answered"]},
            )
            self.assertEqual(update_response.status_code, 200)
            self.assertEqual(update_response.json()["item"]["status"], "resolved")
            self.assertEqual(update_response.json()["item"]["priority"], "low")

            user_detail = client.get(f"/api/support/tickets/{ticket['id']}", headers={"Authorization": "Bearer user-token"})
            self.assertEqual(user_detail.status_code, 200)
            self.assertEqual(user_detail.json()["item"]["message_count"], 2)
            self.assertGreaterEqual(len(fake_email.sent), 2)
            admin_detail = client.get(f"/api/admin/support/tickets/{ticket['id']}", headers={"Authorization": "Bearer admin-token"})
            notifications = admin_detail.json()["item"]["notifications"]
            self.assertTrue(any(item["status"] == "sent" for item in notifications))

    def test_other_user_cannot_read_ticket(self):
        ctx = self.create_context()
        client: TestClient = ctx["client"]

        with self.patch_api(ctx):
            create_response = client.post(
                "/api/support/tickets",
                headers={"Authorization": "Bearer user-token"},
                json={"subject": "Private", "message": "private body"},
            )
            ticket_id = create_response.json()["item"]["id"]
            other_response = client.get(f"/api/support/tickets/{ticket_id}", headers={"Authorization": "Bearer other-token"})

        self.assertEqual(other_response.status_code, 404)

    def test_user_uploads_ticket_attachment(self):
        ctx = self.create_context()
        client: TestClient = ctx["client"]

        with self.patch_api(ctx):
            create_response = client.post(
                "/api/support/tickets",
                headers={"Authorization": "Bearer user-token"},
                json={"subject": "Screenshot", "message": "Please inspect"},
            )
            ticket_id = create_response.json()["item"]["id"]
            upload_response = client.post(
                f"/api/support/tickets/{ticket_id}/attachments",
                headers={"Authorization": "Bearer user-token"},
                data={"message": "The failing screen is attached"},
                files={"file": ("screenshot.png", b"fake-png", "image/png")},
            )

        self.assertEqual(upload_response.status_code, 200)
        item = upload_response.json()["item"]
        self.assertEqual(item["message_count"], 2)
        attachment = item["messages"][-1]["attachments"][0]
        self.assertEqual(attachment["filename"], "screenshot.png")
        self.assertEqual(attachment["content_type"], "image/png")
        self.assertIn("/assets/support/", attachment["url"])

    def test_other_user_cannot_upload_ticket_attachment(self):
        ctx = self.create_context()
        client: TestClient = ctx["client"]

        with self.patch_api(ctx):
            create_response = client.post(
                "/api/support/tickets",
                headers={"Authorization": "Bearer user-token"},
                json={"subject": "Private", "message": "private body"},
            )
            ticket_id = create_response.json()["item"]["id"]
            other_response = client.post(
                f"/api/support/tickets/{ticket_id}/attachments",
                headers={"Authorization": "Bearer other-token"},
                data={"message": "peek"},
                files={"file": ("peek.txt", b"peek", "text/plain")},
            )

        self.assertEqual(other_response.status_code, 404)

    def test_admin_internal_attachment_is_hidden_from_user_api(self):
        ctx = self.create_context()
        client: TestClient = ctx["client"]

        with self.patch_api(ctx):
            create_response = client.post(
                "/api/support/tickets",
                headers={"Authorization": "Bearer user-token"},
                json={"subject": "Internal", "message": "private body"},
            )
            ticket_id = create_response.json()["item"]["id"]
            admin_upload = client.post(
                f"/api/admin/support/tickets/{ticket_id}/attachments",
                headers={"Authorization": "Bearer admin-token"},
                data={"message": "internal only", "internal": "true"},
                files={"file": ("admin-note.txt", b"note", "text/plain")},
            )
            user_detail = client.get(f"/api/support/tickets/{ticket_id}", headers={"Authorization": "Bearer user-token"})

        self.assertEqual(admin_upload.status_code, 200)
        self.assertIn("admin-note.txt", str(admin_upload.json()["item"]["messages"]))
        self.assertNotIn("admin-note.txt", str(user_detail.json()["item"]["messages"]))


if __name__ == "__main__":
    unittest.main()
