from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Migration:
    """A lightweight schema migration version marker.

    The current first-generation runner keeps DDL source-of-truth in SQLAlchemy
    models and records which schema generations have been applied. Future
    versions can attach explicit DDL functions without changing the public CLI.
    """

    version: str
    name: str
    description: str = ""


ALL_MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version="0001_initial_storage_tables",
        name="Initial SQL storage tables",
        description="accounts, auth_keys and generic storage_collections tables",
    ),
    Migration(
        version="0002_quota_ledger_table",
        name="Quota ledger table",
        description="append-only quota_ledger audit table",
    ),
    Migration(
        version="0003_dedicated_commercial_tables",
        name="Dedicated commercial collection tables",
        description="users, packages, cdks, redemptions, orders, payments, image_jobs and image_assets tables",
    ),
    Migration(
        version="0004_audit_logs_table",
        name="Persistent audit log table",
        description="audit_logs table for admin/security operation audit trails",
    ),
    Migration(
        version="0005_launch_evidence_table",
        name="Launch evidence table",
        description="launch_evidence table for production deployment verification reports",
    ),
    Migration(
        version="0006_auth_sessions_tokens_tables",
        name="Auth sessions and action tokens tables",
        description="auth_sessions and auth_action_tokens tables for cookie sessions, email verification and password reset",
    ),
    Migration(
        version="0007_support_tickets_table",
        name="Support tickets table",
        description="support_tickets table for customer service workflow and message history",
    ),
)
