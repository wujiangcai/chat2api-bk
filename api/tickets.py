from __future__ import annotations

import os

from fastapi import APIRouter, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from api.support import require_admin, require_identity, resolve_image_base_url
from services.email_service import email_service
from services.log_service import LOG_TYPE_ACCOUNT, log_service
from services.support_ticket_service import support_ticket_service


class TicketCreateRequest(BaseModel):
    subject: str = Field(default="", max_length=160)
    message: str = Field(default="", max_length=5000)
    category: str = "other"
    priority: str = "normal"
    metadata: dict[str, object] | None = None


class TicketMessageRequest(BaseModel):
    message: str = Field(default="", max_length=5000)
    internal: bool = False


class AdminTicketUpdateRequest(BaseModel):
    status: str | None = None
    priority: str | None = None
    assignee_id: str | None = None
    assignee_name: str | None = None
    tags: list[str] | None = None
    metadata: dict[str, object] | None = None


def _audit_ticket_action(action: str, summary: str, **detail: object) -> None:
    safe_detail = {key: value for key, value in detail.items() if key not in {"message", "token", "password", "secret"}}
    log_service.add(LOG_TYPE_ACCOUNT, summary, {"action": action, **safe_detail})


def _ticket_email_notifications_enabled() -> bool:
    raw = os.getenv("SUPPORT_TICKET_EMAIL_NOTIFICATIONS_ENABLED", "false")
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _notify_ticket_user(ticket: dict[str, object], *, event: str, message: str) -> None:
    recipient = str(ticket.get("email") or "").strip().lower()
    ticket_id = str(ticket.get("id") or "").strip()
    if not ticket_id or not recipient:
        return
    if not _ticket_email_notifications_enabled():
        support_ticket_service.record_notification(
            ticket_id,
            event=event,
            channel="email",
            recipient=recipient,
            status="skipped",
            message="support ticket email notifications disabled",
        )
        return
    try:
        result = email_service.send_support_ticket_update(
            to=recipient,
            ticket_id=ticket_id,
            ticket_subject=str(ticket.get("subject") or ""),
            update_message=message,
            event=event,
        )
        support_ticket_service.record_notification(
            ticket_id,
            event=event,
            channel="email",
            recipient=recipient,
            status="sent" if result.sent else "skipped",
            message=result.message,
        )
    except Exception as exc:
        support_ticket_service.record_notification(
            ticket_id,
            event=event,
            channel="email",
            recipient=recipient,
            status="failed",
            message=str(exc),
        )


async def _read_attachment(file: UploadFile) -> tuple[str, str, bytes]:
    data = await file.read()
    filename = str(file.filename or "attachment").strip() or "attachment"
    content_type = str(file.content_type or "").strip()
    return filename, content_type, data


def create_router() -> APIRouter:
    router = APIRouter()

    @router.post("/api/support/tickets")
    async def create_ticket(body: TicketCreateRequest, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        try:
            item = await run_in_threadpool(
                support_ticket_service.create_ticket,
                identity,
                subject=body.subject,
                message=body.message,
                category=body.category,
                priority=body.priority,
                metadata=body.metadata,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        _audit_ticket_action("support.ticket.create", "support ticket created", ticket_id=item.get("id"), category=item.get("category"))
        _notify_ticket_user(item, event="ticket_created", message="Your support ticket was received. Our team will review it as soon as possible.")
        return {"item": item}

    @router.get("/api/support/tickets")
    async def list_my_tickets(
            status: str = "",
            priority: str = "",
            limit: int = Query(default=100, ge=1, le=500),
            authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        items = await run_in_threadpool(
            support_ticket_service.list_tickets,
            identity,
            status=status,
            priority=priority,
            limit=limit,
        )
        return {"items": items}

    @router.get("/api/support/tickets/{ticket_id}")
    async def get_my_ticket(ticket_id: str, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        item = await run_in_threadpool(support_ticket_service.get_ticket, ticket_id, identity)
        if item is None:
            raise HTTPException(status_code=404, detail={"error": "support ticket not found"})
        return {"item": item}

    @router.post("/api/support/tickets/{ticket_id}/messages")
    async def add_my_ticket_message(ticket_id: str, body: TicketMessageRequest, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        try:
            item = await run_in_threadpool(
                support_ticket_service.add_message,
                ticket_id,
                identity,
                message=body.message,
                internal=False,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        if item is None:
            raise HTTPException(status_code=404, detail={"error": "support ticket not found"})
        _audit_ticket_action("support.ticket.message", "support ticket user message added", ticket_id=ticket_id)
        return {"item": item}

    @router.post("/api/support/tickets/{ticket_id}/attachments")
    async def add_my_ticket_attachment(
            ticket_id: str,
            request: Request,
            file: UploadFile = File(...),
            message: str = Form(""),
            authorization: str | None = Header(default=None),
    ):
        identity = require_identity(authorization)
        filename, content_type, data = await _read_attachment(file)
        try:
            item = await run_in_threadpool(
                support_ticket_service.add_attachment,
                ticket_id,
                identity,
                filename=filename,
                content_type=content_type,
                data=data,
                message=message,
                internal=False,
                base_url=resolve_image_base_url(request),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        if item is None:
            raise HTTPException(status_code=404, detail={"error": "support ticket not found"})
        _audit_ticket_action("support.ticket.attachment", "support ticket attachment uploaded", ticket_id=ticket_id, filename=filename, content_type=content_type, size_bytes=len(data))
        return {"item": item}

    @router.get("/api/admin/support/tickets")
    async def list_admin_tickets(
            status: str = "",
            priority: str = "",
            limit: int = Query(default=200, ge=1, le=1000),
            authorization: str | None = Header(default=None),
    ):
        actor = require_admin(authorization)
        items = await run_in_threadpool(
            support_ticket_service.list_tickets,
            actor,
            status=status,
            priority=priority,
            limit=limit,
        )
        return {"items": items}

    @router.get("/api/admin/support/tickets/{ticket_id}")
    async def get_admin_ticket(ticket_id: str, authorization: str | None = Header(default=None)):
        actor = require_admin(authorization)
        item = await run_in_threadpool(support_ticket_service.get_ticket, ticket_id, actor)
        if item is None:
            raise HTTPException(status_code=404, detail={"error": "support ticket not found"})
        return {"item": item}

    @router.post("/api/admin/support/tickets/{ticket_id}")
    async def update_admin_ticket(ticket_id: str, body: AdminTicketUpdateRequest, authorization: str | None = Header(default=None)):
        actor = require_admin(authorization)
        try:
            item = await run_in_threadpool(
                support_ticket_service.update_ticket,
                ticket_id,
                actor,
                status=body.status,
                priority=body.priority,
                assignee_id=body.assignee_id,
                assignee_name=body.assignee_name,
                tags=body.tags,
                metadata=body.metadata,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        if item is None:
            raise HTTPException(status_code=404, detail={"error": "support ticket not found"})
        _audit_ticket_action("support.ticket.update", "support ticket updated by admin", ticket_id=ticket_id, status=item.get("status"), priority=item.get("priority"))
        if body.status in {"resolved", "closed"}:
            _notify_ticket_user(item, event=f"ticket_{body.status}", message=f"Your support ticket status changed to {body.status}.")
        return {"item": item}

    @router.post("/api/admin/support/tickets/{ticket_id}/messages")
    async def add_admin_ticket_message(ticket_id: str, body: TicketMessageRequest, authorization: str | None = Header(default=None)):
        actor = require_admin(authorization)
        try:
            item = await run_in_threadpool(
                support_ticket_service.add_message,
                ticket_id,
                actor,
                message=body.message,
                internal=body.internal,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        if item is None:
            raise HTTPException(status_code=404, detail={"error": "support ticket not found"})
        _audit_ticket_action("support.ticket.admin_message", "support ticket admin message added", ticket_id=ticket_id, internal=body.internal)
        if not body.internal:
            _notify_ticket_user(item, event="admin_reply", message=body.message)
        return {"item": item}

    @router.post("/api/admin/support/tickets/{ticket_id}/attachments")
    async def add_admin_ticket_attachment(
            ticket_id: str,
            request: Request,
            file: UploadFile = File(...),
            message: str = Form(""),
            internal: bool = Form(False),
            authorization: str | None = Header(default=None),
    ):
        actor = require_admin(authorization)
        filename, content_type, data = await _read_attachment(file)
        try:
            item = await run_in_threadpool(
                support_ticket_service.add_attachment,
                ticket_id,
                actor,
                filename=filename,
                content_type=content_type,
                data=data,
                message=message,
                internal=internal,
                base_url=resolve_image_base_url(request),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        if item is None:
            raise HTTPException(status_code=404, detail={"error": "support ticket not found"})
        _audit_ticket_action("support.ticket.admin_attachment", "support ticket admin attachment uploaded", ticket_id=ticket_id, internal=internal, filename=filename, content_type=content_type, size_bytes=len(data))
        if not internal:
            _notify_ticket_user(item, event="admin_attachment", message=message or f"Support attachment uploaded: {filename}")
        return {"item": item}

    return router
