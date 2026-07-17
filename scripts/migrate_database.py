#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine

# Add project root to Python path when running from scripts/ directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.storage.database_storage import Base, DatabaseStorageBackend
from services.storage.migrations import get_migration_status, run_migrations

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE_URL = f"sqlite:///{(PROJECT_ROOT / 'data' / 'accounts.db').as_posix()}"


def _mask_password(url: str) -> str:
    return DatabaseStorageBackend._mask_password(url)


def _database_url(value: str | None) -> str:
    candidate = (value or os.getenv("DATABASE_URL") or DEFAULT_DATABASE_URL).strip()
    if not candidate:
        raise SystemExit("database url is required")
    return candidate


def _prepare_sqlite_parent(database_url: str) -> None:
    if not database_url.startswith("sqlite:///"):
        return
    path_value = database_url.removeprefix("sqlite:///")
    if path_value in {"", ":memory:"} or path_value.startswith("file:"):
        return
    Path(path_value).expanduser().parent.mkdir(parents=True, exist_ok=True)


def _print_status(status: dict[str, Any], *, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return
    print(
        "Schema migrations: "
        f"{status['applied_count']} applied / {status['pending_count']} pending / {status['known_count']} known"
    )
    for item in status["migrations"]:
        suffix = f" @ {item['applied_at']}" if item.get("applied_at") else ""
        print(f"- [{item['status']}] {item['version']} {item['name']}{suffix}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run or inspect database schema migrations for chatgpt2api-bk.",
    )
    parser.add_argument(
        "--database-url",
        help="SQLAlchemy database URL. Defaults to DATABASE_URL or data/accounts.db SQLite.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Only print current migration status; do not create application tables.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show pending migrations without applying them.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Print machine-readable JSON output.",
    )
    args = parser.parse_args()

    database_url = _database_url(args.database_url)
    _prepare_sqlite_parent(database_url)
    engine = create_engine(database_url, pool_pre_ping=True, pool_recycle=3600)
    try:
        if args.status:
            status = get_migration_status(engine, ensure_table=False)
            output = {"database_url": _mask_password(database_url), **status}
            _print_status(output, as_json=args.as_json)
            return 0

        result = run_migrations(
            engine,
            dry_run=args.dry_run,
            schema_initializer=lambda target_engine: Base.metadata.create_all(target_engine),
        )
        status = get_migration_status(engine, ensure_table=not args.dry_run)
        output = {
            "database_url": _mask_password(database_url),
            "dry_run": args.dry_run,
            "applied_now": result["applied"],
            "pending_before_run": result["pending"],
            **status,
        }
        if args.as_json:
            print(json.dumps(output, ensure_ascii=False, indent=2))
        else:
            if args.dry_run:
                print(f"Dry run for {_mask_password(database_url)}")
                if result["pending"]:
                    print("Pending migrations: " + ", ".join(result["pending"]))
                else:
                    print("No pending migrations.")
            else:
                print(f"Migrated database {_mask_password(database_url)}")
                if result["applied"]:
                    for item in result["applied"]:
                        print(f"- applied {item['version']} {item['name']} @ {item['applied_at']}")
                else:
                    print("No pending migrations.")
            _print_status(status, as_json=False)
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
