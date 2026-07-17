from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, inspect

from services.storage.database_storage import Base, DatabaseStorageBackend
from services.storage.migrations import ALL_MIGRATIONS, get_migration_status, run_migrations


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class DatabaseMigrationTests(unittest.TestCase):
    def sqlite_url(self, directory: Path, name: str = "migrations.sqlite3") -> str:
        return f"sqlite:///{(directory / name).as_posix()}"

    def test_run_migrations_creates_tracking_table_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = create_engine(self.sqlite_url(Path(tmp)))
            try:
                result = run_migrations(
                    engine,
                    schema_initializer=lambda target_engine: Base.metadata.create_all(target_engine),
                )
                self.assertEqual([item["version"] for item in result["applied"]], [m.version for m in ALL_MIGRATIONS])

                inspector = inspect(engine)
                self.assertIn("schema_migrations", inspector.get_table_names())
                self.assertIn("orders", inspector.get_table_names())
                self.assertIn("image_assets", inspector.get_table_names())
                self.assertIn("auth_sessions", inspector.get_table_names())
                self.assertIn("auth_action_tokens", inspector.get_table_names())

                status = get_migration_status(engine)
                self.assertEqual(status["applied_count"], len(ALL_MIGRATIONS))
                self.assertEqual(status["pending_count"], 0)

                second = run_migrations(
                    engine,
                    schema_initializer=lambda target_engine: Base.metadata.create_all(target_engine),
                )
                self.assertEqual(second["applied"], [])
                self.assertEqual(second["pending"], [])
            finally:
                engine.dispose()

    def test_database_storage_initialization_runs_migrations_and_reports_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = DatabaseStorageBackend(self.sqlite_url(Path(tmp)))
            try:
                health = storage.health_check()
                self.assertEqual(health["status"], "healthy")
                self.assertEqual(health["schema_migration_count"], len(ALL_MIGRATIONS))
                self.assertEqual([item["version"] for item in health["schema_migrations"]], [m.version for m in ALL_MIGRATIONS])
            finally:
                storage.engine.dispose()

    def test_migrate_database_cli_dry_run_apply_and_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_url = self.sqlite_url(Path(tmp), "cli.sqlite3")
            script = PROJECT_ROOT / "scripts" / "migrate_database.py"

            dry_run = subprocess.run(
                [sys.executable, str(script), "--database-url", db_url, "--dry-run", "--json"],
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
                check=True,
            )
            dry_run_payload = json.loads(dry_run.stdout)
            self.assertTrue(dry_run_payload["dry_run"])
            self.assertEqual(dry_run_payload["pending_count"], len(ALL_MIGRATIONS))
            self.assertEqual(dry_run_payload["applied_now"], [])
            dry_run_engine = create_engine(db_url)
            try:
                self.assertNotIn("schema_migrations", inspect(dry_run_engine).get_table_names())
            finally:
                dry_run_engine.dispose()

            applied = subprocess.run(
                [sys.executable, str(script), "--database-url", db_url, "--json"],
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
                check=True,
            )
            applied_payload = json.loads(applied.stdout)
            self.assertFalse(applied_payload["dry_run"])
            self.assertEqual(len(applied_payload["applied_now"]), len(ALL_MIGRATIONS))
            self.assertEqual(applied_payload["pending_count"], 0)

            status = subprocess.run(
                [sys.executable, str(script), "--database-url", db_url, "--status", "--json"],
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
                check=True,
            )
            status_payload = json.loads(status.stdout)
            self.assertEqual(status_payload["applied_count"], len(ALL_MIGRATIONS))
            self.assertEqual(status_payload["pending_count"], 0)


if __name__ == "__main__":
    unittest.main()
