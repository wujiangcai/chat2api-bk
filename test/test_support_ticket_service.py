from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from services.object_storage import LocalObjectStorage
from services.storage.json_storage import JSONStorageBackend
from services.support_ticket_service import SupportTicketService


class SupportTicketServiceTests(unittest.TestCase):
    def create_service(self, *, with_object_storage: bool = False) -> SupportTicketService:
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        root = Path(tmp_dir.name)
        storage = JSONStorageBackend(root / "accounts.json", root / "auth_keys.json")
        object_storage = LocalObjectStorage(root / "assets") if with_object_storage else None
        return SupportTicketService(storage, object_storage=object_storage)

    def test_user_ticket_admin_reply_and_internal_note_visibility(self):
        service = self.create_service()
        user = {"role": "user", "user_id": "usr_1", "email": "u@example.com", "name": "User"}
        admin = {"role": "admin", "id": "adm_1", "email": "admin@example.com", "name": "Admin"}

        ticket = service.create_ticket(
            user,
            subject="Need invoice",
            message="Please help with my invoice.",
            category="billing",
            priority="high",
        )
        self.assertEqual(ticket["status"], "open")
        self.assertEqual(ticket["category"], "billing")
        self.assertEqual(ticket["priority"], "high")
        self.assertEqual(ticket["message_count"], 1)

        updated = service.add_message(ticket["id"], admin, message="Internal triage note", internal=True)
        self.assertIsNotNone(updated)
        self.assertEqual(updated["status"], "in_progress")
        self.assertEqual(updated["message_count"], 2)

        public_view = service.get_ticket(ticket["id"], user)
        self.assertIsNotNone(public_view)
        self.assertEqual(public_view["message_count"], 1)
        self.assertEqual(len(public_view["messages"]), 1)
        self.assertNotIn("Internal triage note", str(public_view["messages"]))

        service.add_message(ticket["id"], admin, message="We will email it today.")
        public_view = service.get_ticket(ticket["id"], user)
        self.assertEqual(public_view["message_count"], 2)
        self.assertIn("We will email it today.", str(public_view["messages"]))

    def test_user_can_reopen_resolved_ticket_by_replying(self):
        service = self.create_service()
        user = {"role": "user", "user_id": "usr_2", "email": "u2@example.com"}
        admin = {"role": "admin", "id": "adm_1"}
        ticket = service.create_ticket(user, subject="Image failed", message="A job failed.", category="image")

        resolved = service.update_ticket(ticket["id"], admin, status="resolved")
        self.assertEqual(resolved["status"], "resolved")
        reopened = service.add_message(ticket["id"], user, message="Still happening.")
        self.assertEqual(reopened["status"], "open")

    def test_cross_user_access_is_denied(self):
        service = self.create_service()
        owner = {"role": "user", "user_id": "usr_owner", "email": "owner@example.com"}
        other = {"role": "user", "user_id": "usr_other", "email": "other@example.com"}
        ticket = service.create_ticket(owner, subject="Account", message="Help", category="account")

        self.assertIsNone(service.get_ticket(ticket["id"], other))
        self.assertIsNone(service.add_message(ticket["id"], other, message="peek"))

    def test_sla_overdue_and_first_response_tracking(self):
        service = self.create_service()
        admin = {"role": "admin", "id": "adm_1"}
        old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        due = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        service.storage.save_collection(
            "support_tickets",
            [
                {
                    "id": "tic_sla",
                    "user_id": "usr_sla",
                    "email": "sla@example.com",
                    "subject": "SLA",
                    "category": "api",
                    "priority": "high",
                    "status": "open",
                    "created_at": old,
                    "updated_at": old,
                    "last_message_at": old,
                    "first_response_due_at": due,
                    "resolution_due_at": due,
                    "messages": [{"id": "msg_sla", "author_type": "user", "author_id": "usr_sla", "body": "help", "created_at": old}],
                }
            ],
        )

        ticket = service.get_ticket("tic_sla", admin)
        self.assertEqual(ticket["sla_status"], "response_overdue")
        self.assertGreater(ticket["response_overdue_seconds"], 0)

        replied = service.add_message("tic_sla", admin, message="We are checking.")
        self.assertEqual(replied["status"], "in_progress")
        self.assertIsNotNone(replied["first_response_at"])

    def test_notification_history_is_recorded_for_admin_view(self):
        service = self.create_service()
        user = {"role": "user", "user_id": "usr_notify", "email": "notify@example.com"}
        admin = {"role": "admin", "id": "adm_1"}
        ticket = service.create_ticket(user, subject="Notify", message="hello")

        updated = service.record_notification(
            ticket["id"],
            event="admin_reply",
            channel="email",
            recipient="notify@example.com",
            status="sent",
            message="ok",
        )

        self.assertEqual(updated["notifications"][0]["status"], "sent")
        public_view = service.get_ticket(ticket["id"], user)
        self.assertEqual(public_view["notifications"], [])
        admin_view = service.get_ticket(ticket["id"], admin)
        self.assertEqual(admin_view["notifications"][0]["event"], "admin_reply")

    def test_user_can_upload_attachment(self):
        service = self.create_service(with_object_storage=True)
        user = {"role": "user", "user_id": "usr_attach", "email": "attach@example.com"}
        ticket = service.create_ticket(user, subject="Screenshot", message="Initial issue")

        updated = service.add_attachment(
            ticket["id"],
            user,
            filename="screen shot.png",
            content_type="image/png",
            data=b"fake-png",
            message="See screenshot",
            base_url="https://app.example.com",
        )

        self.assertIsNotNone(updated)
        self.assertEqual(updated["message_count"], 2)
        attachment = updated["messages"][-1]["attachments"][0]
        self.assertEqual(attachment["filename"], "screen_shot.png")
        self.assertEqual(attachment["content_type"], "image/png")
        self.assertEqual(attachment["size_bytes"], len(b"fake-png"))
        self.assertTrue(str(attachment["url"]).startswith("https://app.example.com/assets/support/"))
        self.assertEqual(service.stats()["attachments_total"], 1)

    def test_admin_internal_attachment_is_hidden_from_user(self):
        service = self.create_service(with_object_storage=True)
        user = {"role": "user", "user_id": "usr_hidden", "email": "hidden@example.com"}
        admin = {"role": "admin", "id": "adm_1", "email": "admin@example.com"}
        ticket = service.create_ticket(user, subject="Private attachment", message="Help")

        admin_view = service.add_attachment(
            ticket["id"],
            admin,
            filename="triage.txt",
            content_type="text/plain",
            data=b"internal notes",
            message="Internal evidence",
            internal=True,
        )
        self.assertEqual(admin_view["message_count"], 2)
        self.assertEqual(admin_view["messages"][-1]["attachments"][0]["filename"], "triage.txt")

        public_view = service.get_ticket(ticket["id"], user)
        self.assertEqual(public_view["message_count"], 1)
        self.assertNotIn("triage.txt", str(public_view["messages"]))

    def test_attachment_validation_rejects_type_and_size(self):
        service = self.create_service(with_object_storage=True)
        user = {"role": "user", "user_id": "usr_limits", "email": "limits@example.com"}
        ticket = service.create_ticket(user, subject="Limits", message="Help")

        with patch.dict("os.environ", {"SUPPORT_TICKET_ATTACHMENT_ALLOWED_TYPES": "image/png"}, clear=False):
            with self.assertRaisesRegex(ValueError, "content type"):
                service.add_attachment(
                    ticket["id"],
                    user,
                    filename="payload.exe",
                    content_type="application/octet-stream",
                    data=b"binary",
                )

        with patch.dict("os.environ", {"SUPPORT_TICKET_ATTACHMENT_MAX_BYTES": "3"}, clear=False):
            with self.assertRaisesRegex(ValueError, "maximum size"):
                service.add_attachment(
                    ticket["id"],
                    user,
                    filename="too-large.png",
                    content_type="image/png",
                    data=b"1234",
                )


if __name__ == "__main__":
    unittest.main()
