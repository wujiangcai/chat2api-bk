from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .versions import ALL_MIGRATIONS, Migration

SCHEMA_MIGRATIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version VARCHAR(64) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    applied_at VARCHAR(64) NOT NULL
)
"""


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def ensure_schema_migrations_table(engine: Engine) -> None:
    """Create the migration tracking table if it does not exist."""

    with engine.begin() as connection:
        connection.execute(text(SCHEMA_MIGRATIONS_TABLE_SQL))


def get_applied_migrations(engine: Engine, *, ensure_table: bool = True) -> list[dict[str, str]]:
    """Return applied schema migration records ordered by version."""

    if ensure_table:
        ensure_schema_migrations_table(engine)
    try:
        with engine.begin() as connection:
            rows = connection.execute(
                text("SELECT version, name, applied_at FROM schema_migrations ORDER BY version ASC")
            ).mappings().all()
    except SQLAlchemyError:
        if ensure_table:
            raise
        rows = []
    return [
        {
            "version": str(row["version"]),
            "name": str(row["name"]),
            "applied_at": str(row["applied_at"]),
        }
        for row in rows
    ]


def get_migration_status(engine: Engine, *, ensure_table: bool = True) -> dict[str, Any]:
    """Return all known migrations with applied/pending status."""

    applied_rows = get_applied_migrations(engine, ensure_table=ensure_table)
    applied_by_version = {row["version"]: row for row in applied_rows}
    migrations = []
    for migration in ALL_MIGRATIONS:
        applied = applied_by_version.get(migration.version)
        migrations.append(
            {
                "version": migration.version,
                "name": migration.name,
                "description": migration.description,
                "status": "applied" if applied else "pending",
                "applied_at": applied.get("applied_at") if applied else None,
            }
        )
    return {
        "known_count": len(ALL_MIGRATIONS),
        "applied_count": len(applied_rows),
        "pending_count": len([item for item in migrations if item["status"] == "pending"]),
        "migrations": migrations,
    }


def run_migrations(
    engine: Engine,
    *,
    dry_run: bool = False,
    schema_initializer: Callable[[Engine], None] | None = None,
    migrations: tuple[Migration, ...] = ALL_MIGRATIONS,
) -> dict[str, Any]:
    """Apply pending lightweight schema migrations.

    The runner intentionally keeps the initial implementation conservative:
    SQLAlchemy models remain the DDL source of truth, while this function adds a
    durable, auditable `schema_migrations` table and records each schema
    generation exactly once. Passing `schema_initializer` lets the app/CLI call
    `Base.metadata.create_all(engine)` only when pending migrations exist.
    """

    if not dry_run:
        ensure_schema_migrations_table(engine)
    applied_versions = {
        row["version"]
        for row in get_applied_migrations(engine, ensure_table=not dry_run)
    }
    pending = [migration for migration in migrations if migration.version not in applied_versions]

    result: dict[str, Any] = {
        "dry_run": dry_run,
        "applied": [],
        "pending": [migration.version for migration in pending],
        "already_applied": sorted(applied_versions),
    }
    if dry_run or not pending:
        return result

    if schema_initializer is not None:
        schema_initializer(engine)
        ensure_schema_migrations_table(engine)

    for migration in pending:
        applied_at = _utc_now_iso()
        try:
            with engine.begin() as connection:
                existing = connection.execute(
                    text("SELECT version FROM schema_migrations WHERE version = :version"),
                    {"version": migration.version},
                ).first()
                if existing is not None:
                    continue
                connection.execute(
                    text(
                        "INSERT INTO schema_migrations (version, name, applied_at) "
                        "VALUES (:version, :name, :applied_at)"
                    ),
                    {
                        "version": migration.version,
                        "name": migration.name,
                        "applied_at": applied_at,
                    },
                )
        except IntegrityError:
            # Another process may have inserted the row after our SELECT. Treat
            # the migration as already applied to keep startup idempotent.
            continue
        result["applied"].append(
            {
                "version": migration.version,
                "name": migration.name,
                "applied_at": applied_at,
            }
        )

    return result


