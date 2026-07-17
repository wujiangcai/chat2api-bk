from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from services.backup_service import BackupOptions, RestoreOptions, create_backup, restore_backup, verify_backup
from services.storage.database_storage import DatabaseStorageBackend

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class BackupServiceTests(unittest.TestCase):
    def test_create_verify_and_restore_sqlite_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project_root = base / "project"
            data_dir = project_root / "data"
            output_dir = data_dir / "backups"
            data_dir.mkdir(parents=True)
            (data_dir / "assets" / "2026").mkdir(parents=True)
            (data_dir / "assets" / "2026" / "asset.txt").write_text("asset-data", encoding="utf-8")
            (data_dir / "logs.jsonl").write_text('{"ok":true}\n', encoding="utf-8")
            (project_root / "config.json").write_text(
                json.dumps({"auth-key": "super-secret", "base_url": "https://img.example.com"}),
                encoding="utf-8",
            )

            db_path = data_dir / "accounts.db"
            db_url = f"sqlite:///{db_path.as_posix()}"
            storage = DatabaseStorageBackend(db_url)
            try:
                storage.save_collection(
                    "packages",
                    [
                        {
                            "id": "pkg_1",
                            "name": "Pro",
                            "quota": 100,
                            "price_cents": 990,
                            "currency": "CNY",
                            "created_at": "2026-07-07T00:00:00+00:00",
                            "updated_at": "2026-07-07T00:00:00+00:00",
                        }
                    ],
                )
                storage.append_collection_item(
                    "audit_logs",
                    {
                        "id": "aud_1",
                        "action": "backup.test",
                        "status": "succeeded",
                        "actor": {"type": "admin", "id": "adm_1"},
                        "target": {"type": "backup", "id": "test"},
                        "created_at": "2026-07-07T00:00:00+00:00",
                    },
                )
            finally:
                storage.engine.dispose()

            result = create_backup(
                BackupOptions(
                    project_root=project_root,
                    data_dir=data_dir,
                    output_dir=output_dir,
                    database_url=db_url,
                    note="unit-test",
                )
            )
            self.assertTrue(result["ok"])
            backup_file = Path(result["backup_file"])
            self.assertTrue(backup_file.exists())

            verification = verify_backup(backup_file)
            self.assertTrue(verification["ok"], verification)
            manifest = verification["manifest"]
            self.assertTrue(manifest["database"]["included"])
            archived_paths = {item["path"] for item in manifest["files"]}
            self.assertIn("data/assets/2026/asset.txt", archived_paths)
            self.assertIn("data/logs.jsonl", archived_paths)
            self.assertIn("database/sqlite.sqlite3", archived_paths)
            self.assertIn("config/config.sanitized.json", archived_paths)
            self.assertNotIn("data/backups", "\n".join(archived_paths))

            with zipfile.ZipFile(backup_file, "r") as archive:
                sanitized_config = json.loads(archive.read("config/config.sanitized.json").decode("utf-8"))
            self.assertEqual(sanitized_config["auth-key"], "[REDACTED]")
            self.assertEqual(sanitized_config["base_url"], "https://img.example.com")

            restore_dir = base / "restore" / "data"
            restore_db = restore_dir / "accounts.db"
            restore_result = restore_backup(
                RestoreOptions(
                    backup_file=backup_file,
                    restore_data_dir=restore_dir,
                    database_url=f"sqlite:///{restore_db.as_posix()}",
                    overwrite=True,
                )
            )
            self.assertTrue(restore_result["ok"])
            self.assertEqual((restore_dir / "assets" / "2026" / "asset.txt").read_text(encoding="utf-8"), "asset-data")
            self.assertEqual((restore_dir / "logs.jsonl").read_text(encoding="utf-8"), '{"ok":true}\n')

            connection = sqlite3.connect(restore_db)
            try:
                package_count = connection.execute("SELECT COUNT(*) FROM packages").fetchone()[0]
                audit_count = connection.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(package_count, 1)
            self.assertEqual(audit_count, 1)

    def test_backup_cli_verify_json_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project_root = base / "project"
            data_dir = project_root / "data"
            output_dir = data_dir / "backups"
            data_dir.mkdir(parents=True)
            (data_dir / "accounts.json").write_text("[]\n", encoding="utf-8")
            (project_root / "config.json").write_text("{}\n", encoding="utf-8")
            result = create_backup(
                BackupOptions(
                    project_root=project_root,
                    data_dir=data_dir,
                    output_dir=output_dir,
                    skip_database=True,
                )
            )
            script = PROJECT_ROOT / "scripts" / "backup_data.py"
            completed = subprocess.run(
                [sys.executable, str(script), "verify", result["backup_file"], "--json"],
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
                check=True,
            )
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"])
            self.assertGreaterEqual(payload["checked"], 1)

    def test_restore_refuses_to_overwrite_existing_file_without_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project_root = base / "project"
            data_dir = project_root / "data"
            output_dir = data_dir / "backups"
            data_dir.mkdir(parents=True)
            (data_dir / "accounts.json").write_text("[]\n", encoding="utf-8")
            result = create_backup(
                BackupOptions(
                    project_root=project_root,
                    data_dir=data_dir,
                    output_dir=output_dir,
                    skip_database=True,
                    include_config=False,
                )
            )
            restore_dir = base / "restore"
            restore_dir.mkdir()
            (restore_dir / "accounts.json").write_text("existing\n", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                restore_backup(RestoreOptions(backup_file=Path(result["backup_file"]), restore_data_dir=restore_dir))


if __name__ == "__main__":
    unittest.main()
