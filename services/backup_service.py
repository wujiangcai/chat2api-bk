from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from urllib.parse import urlparse

MANIFEST_NAME = "manifest.json"
BACKUP_SCHEMA_VERSION = 1
DEFAULT_EXCLUDED_DIR_NAMES = {"backups", "__pycache__", ".pytest_cache"}
DEFAULT_EXCLUDED_SUFFIXES = {".tmp", ".temp", ".lock", ".pid"}
SENSITIVE_CONFIG_KEYS = {"auth-key", "auth_key", "token", "secret", "secret_key", "api_key", "password"}


@dataclass(frozen=True)
class BackupOptions:
    project_root: Path
    data_dir: Path
    output_dir: Path
    database_url: str = ""
    include_assets: bool = True
    include_config: bool = True
    skip_database: bool = False
    note: str = ""
    retention_days: int = 0


@dataclass(frozen=True)
class RestoreOptions:
    backup_file: Path
    restore_data_dir: Path
    database_url: str = ""
    overwrite: bool = False
    restore_database: bool = True
    restore_data_files: bool = True


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def mask_database_url(url: str) -> str:
    if "://" not in str(url or ""):
        return str(url or "")
    try:
        parsed = urlparse(url)
        if not parsed.password:
            return url
        username = parsed.username or ""
        credentials = f"{username}:****@" if username else "****@"
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        path = parsed.path or ""
        query = f"?{parsed.query}" if parsed.query else ""
        return f"{parsed.scheme}://{credentials}{host}{port}{path}{query}"
    except Exception:
        return "[masked-database-url]"


def database_type(database_url: str) -> str:
    value = str(database_url or "").strip().lower()
    if value.startswith("sqlite"):
        return "sqlite"
    if value.startswith("postgresql") or value.startswith("postgres://"):
        return "postgresql"
    if value.startswith("mysql"):
        return "mysql"
    return "none" if not value else "unknown"


def sqlite_path_from_url(database_url: str) -> Path | None:
    value = str(database_url or "").strip()
    if not value.startswith("sqlite"):
        return None
    if value in {"sqlite://", "sqlite:///:memory:", "sqlite:///:memory"} or ":memory:" in value:
        return None
    prefix = "sqlite:///"
    if value.startswith(prefix):
        raw_path = value[len(prefix):]
        if raw_path.startswith("/") and len(raw_path) > 3 and raw_path[2] == ":":
            raw_path = raw_path[1:]
        return Path(raw_path)
    prefix = "sqlite://"
    if value.startswith(prefix):
        raw_path = value[len(prefix):]
        return Path(raw_path) if raw_path else None
    return None


def sha256_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def sanitize_config(value: Any, key: str = "") -> Any:
    normalized = str(key or "").strip().lower()
    if normalized in SENSITIVE_CONFIG_KEYS or any(part in normalized for part in ("secret", "token", "password", "api_key")):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item_key): sanitize_config(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [sanitize_config(item) for item in value]
    return value


def should_skip_data_file(path: Path, data_dir: Path, output_dir: Path, include_assets: bool, sqlite_db_path: Path | None) -> bool:
    if not path.is_file():
        return True
    resolved = path.resolve()
    try:
        resolved.relative_to(output_dir.resolve())
        return True
    except ValueError:
        pass
    if sqlite_db_path is not None:
        try:
            if resolved == sqlite_db_path.resolve():
                return True
        except OSError:
            pass
    relative = path.relative_to(data_dir)
    if any(part in DEFAULT_EXCLUDED_DIR_NAMES for part in relative.parts[:-1]):
        return True
    if not include_assets and relative.parts and relative.parts[0] == "assets":
        return True
    if path.suffix.lower() in DEFAULT_EXCLUDED_SUFFIXES:
        return True
    return False


def add_bytes_to_zip(zip_file: zipfile.ZipFile, arcname: str, data: bytes, files: list[dict[str, Any]]) -> None:
    normalized = arcname.replace("\\", "/").lstrip("/")
    zip_file.writestr(normalized, data)
    files.append({"path": normalized, "size": len(data), "sha256": sha256_bytes(data)})


def add_file_to_zip(zip_file: zipfile.ZipFile, source: Path, arcname: str, files: list[dict[str, Any]]) -> None:
    data = source.read_bytes()
    add_bytes_to_zip(zip_file, arcname, data, files)


def sqlite_backup_bytes(source_path: Path) -> bytes:
    if not source_path.exists():
        raise FileNotFoundError(f"sqlite database not found: {source_path}")
    fd, temp_name = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        source = sqlite3.connect(str(source_path))
        target = sqlite3.connect(str(temp_path))
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        return temp_path.read_bytes()
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def postgres_dump_bytes(database_url: str) -> bytes:
    command = ["pg_dump", "--no-owner", "--no-acl", database_url]
    try:
        result = subprocess.run(command, check=True, capture_output=True)
    except FileNotFoundError as exc:
        raise RuntimeError("pg_dump not found; install PostgreSQL client tools or run backup with --skip-database") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else str(exc)
        raise RuntimeError(f"pg_dump failed: {stderr}") from exc
    return result.stdout


def create_backup(options: BackupOptions) -> dict[str, Any]:
    project_root = options.project_root.resolve()
    data_dir = options.data_dir.resolve()
    output_dir = options.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    backup_name = f"chatgpt2api-backup-{utc_stamp()}-{uuid.uuid4().hex[:8]}.zip"
    backup_path = output_dir / backup_name
    db_type = database_type(options.database_url)
    sqlite_db_path = sqlite_path_from_url(options.database_url)
    files: list[dict[str, Any]] = []
    database_info: dict[str, Any] = {
        "type": db_type,
        "included": False,
        "url": mask_database_url(options.database_url),
    }

    with zipfile.ZipFile(backup_path, "w", compression=zipfile.ZIP_DEFLATED) as zip_file:
        for path in sorted(data_dir.rglob("*")):
            if should_skip_data_file(path, data_dir, output_dir, options.include_assets, sqlite_db_path):
                continue
            add_file_to_zip(zip_file, path, f"data/{path.relative_to(data_dir).as_posix()}", files)

        if options.include_config:
            config_path = project_root / "config.json"
            if config_path.is_file():
                try:
                    config_data = json.loads(config_path.read_text(encoding="utf-8"))
                except Exception:
                    config_data = {"error": "config.json is not valid JSON; raw file was not included"}
                sanitized = sanitize_config(config_data)
                add_bytes_to_zip(
                    zip_file,
                    "config/config.sanitized.json",
                    json.dumps(sanitized, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
                    files,
                )

        if not options.skip_database and db_type == "sqlite" and sqlite_db_path is not None:
            add_bytes_to_zip(zip_file, "database/sqlite.sqlite3", sqlite_backup_bytes(sqlite_db_path), files)
            database_info.update({"included": True, "path": "database/sqlite.sqlite3"})
        elif not options.skip_database and db_type == "postgresql":
            add_bytes_to_zip(zip_file, "database/postgres_dump.sql", postgres_dump_bytes(options.database_url), files)
            database_info.update({"included": True, "path": "database/postgres_dump.sql", "format": "plain-sql"})

        manifest = {
            "schema_version": BACKUP_SCHEMA_VERSION,
            "created_at": utc_now_iso(),
            "app": "chatgpt2api-bk",
            "note": options.note,
            "source": {
                "project_root": str(project_root),
                "data_dir": str(data_dir),
            },
            "database": database_info,
            "include_assets": options.include_assets,
            "include_config": options.include_config,
            "files": files,
        }
        zip_file.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

    verification = verify_backup(backup_path)
    pruned = prune_old_backups(output_dir, options.retention_days)
    return {
        "ok": bool(verification["ok"]),
        "backup_file": str(backup_path),
        "file_count": len(files),
        "size_bytes": backup_path.stat().st_size,
        "pruned_old_backups": pruned,
        "manifest": verification.get("manifest"),
    }


def prune_old_backups(output_dir: Path, retention_days: int) -> int:
    try:
        days = int(retention_days or 0)
    except (TypeError, ValueError):
        days = 0
    if days <= 0:
        return 0
    cutoff = datetime.now(UTC).timestamp() - days * 86400
    base = output_dir.resolve()
    removed = 0
    for path in base.glob("chatgpt2api-backup-*.zip"):
        try:
            resolved = path.resolve()
            resolved.relative_to(base)
            if resolved.is_file() and resolved.stat().st_mtime < cutoff:
                resolved.unlink()
                removed += 1
        except (OSError, ValueError):
            continue
    return removed


def load_manifest(zip_file: zipfile.ZipFile) -> dict[str, Any]:
    try:
        data = zip_file.read(MANIFEST_NAME)
    except KeyError as exc:
        raise ValueError("backup manifest.json is missing") from exc
    try:
        manifest = json.loads(data.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("backup manifest.json is invalid") from exc
    if not isinstance(manifest, dict):
        raise ValueError("backup manifest.json must be an object")
    return manifest


def verify_backup(backup_file: Path | str) -> dict[str, Any]:
    backup_path = Path(backup_file)
    missing: list[str] = []
    mismatched: list[str] = []
    checked = 0
    try:
        with zipfile.ZipFile(backup_path, "r") as zip_file:
            manifest = load_manifest(zip_file)
            names = set(zip_file.namelist())
            for item in manifest.get("files", []):
                if not isinstance(item, dict):
                    mismatched.append("<invalid-manifest-file-entry>")
                    continue
                path = str(item.get("path") or "")
                if path not in names:
                    missing.append(path)
                    continue
                data = zip_file.read(path)
                if int(item.get("size") or -1) != len(data) or str(item.get("sha256") or "") != sha256_bytes(data):
                    mismatched.append(path)
                checked += 1
        return {
            "ok": not missing and not mismatched,
            "backup_file": str(backup_path),
            "checked": checked,
            "missing": missing,
            "mismatched": mismatched,
            "manifest": manifest,
        }
    except Exception as exc:
        return {
            "ok": False,
            "backup_file": str(backup_path),
            "checked": checked,
            "missing": missing,
            "mismatched": mismatched,
            "error": str(exc),
        }


def safe_extract_member(zip_file: zipfile.ZipFile, member: str, destination: Path) -> Path:
    destination = destination.resolve()
    target = (destination / member).resolve()
    target.relative_to(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    with zip_file.open(member) as source, target.open("wb") as output:
        shutil.copyfileobj(source, output)
    return target


def restore_sqlite_database(sqlite_archive_path: Path, target_database_url: str, overwrite: bool) -> Path:
    target_path = sqlite_path_from_url(target_database_url)
    if target_path is None:
        raise ValueError("restore database requires a sqlite:/// target --database-url")
    target_path = target_path.resolve()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists() and not overwrite:
        raise FileExistsError(f"target sqlite database exists; pass --overwrite to replace it: {target_path}")
    source = sqlite3.connect(str(sqlite_archive_path))
    target = sqlite3.connect(str(target_path))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return target_path


def restore_backup(options: RestoreOptions) -> dict[str, Any]:
    verification = verify_backup(options.backup_file)
    if not verification.get("ok"):
        raise ValueError(f"backup verification failed: {verification}")
    restore_data_dir = options.restore_data_dir.resolve()
    restore_data_dir.mkdir(parents=True, exist_ok=True)
    restored_files: list[str] = []
    restored_database = ""

    with tempfile.TemporaryDirectory() as temp_dir_name:
        temp_dir = Path(temp_dir_name).resolve()
        with zipfile.ZipFile(options.backup_file, "r") as zip_file:
            manifest = load_manifest(zip_file)
            for item in manifest.get("files", []):
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path") or "")
                if not path:
                    continue
                if options.restore_data_files and path.startswith("data/"):
                    relative = Path(path[len("data/"):])
                    target = (restore_data_dir / relative).resolve()
                    target.relative_to(restore_data_dir)
                    if target.exists() and not options.overwrite:
                        raise FileExistsError(f"target file exists; pass --overwrite to replace it: {target}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zip_file.open(path) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)
                    restored_files.append(str(target))
                elif options.restore_database and path == "database/sqlite.sqlite3":
                    extracted = safe_extract_member(zip_file, path, temp_dir)
                    restored_database = str(restore_sqlite_database(extracted, options.database_url, options.overwrite))

    return {
        "ok": True,
        "backup_file": str(options.backup_file),
        "restore_data_dir": str(restore_data_dir),
        "restored_file_count": len(restored_files),
        "restored_files": restored_files,
        "restored_database": restored_database or None,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create, verify and restore chatgpt2api-bk backups.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="Create a backup archive")
    create.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    create.add_argument("--data-dir", default="")
    create.add_argument("--output-dir", default="")
    create.add_argument("--database-url", default=os.getenv("DATABASE_URL", ""))
    create.add_argument("--skip-database", action="store_true")
    create.add_argument("--no-assets", action="store_true")
    create.add_argument("--no-config", action="store_true")
    create.add_argument("--retention-days", type=int, default=int(os.getenv("BACKUP_RETENTION_DAYS", "0") or "0"))
    create.add_argument("--note", default="")
    create.add_argument("--json", action="store_true", dest="as_json")

    verify = subparsers.add_parser("verify", help="Verify a backup archive")
    verify.add_argument("backup_file")
    verify.add_argument("--json", action="store_true", dest="as_json")

    restore = subparsers.add_parser("restore", help="Restore data files and optional SQLite database from a backup archive")
    restore.add_argument("backup_file")
    restore.add_argument("--restore-to", required=True, help="Target data directory")
    restore.add_argument("--database-url", default="", help="Target sqlite:/// URL for database restore")
    restore.add_argument("--overwrite", action="store_true")
    restore.add_argument("--skip-database", action="store_true")
    restore.add_argument("--skip-data-files", action="store_true")
    restore.add_argument("--json", action="store_true", dest="as_json")

    return parser


def print_result(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if result.get("ok"):
        print("OK")
    else:
        print("FAILED")
    for key, value in result.items():
        if key == "manifest":
            continue
        print(f"{key}: {value}")


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.command == "create":
        project_root = Path(args.project_root).resolve()
        data_dir = Path(args.data_dir).resolve() if args.data_dir else project_root / "data"
        output_dir = Path(args.output_dir or os.getenv("BACKUP_OUTPUT_DIR", "")).resolve() if (args.output_dir or os.getenv("BACKUP_OUTPUT_DIR")) else data_dir / "backups"
        include_assets = os.getenv("BACKUP_INCLUDE_ASSETS", "true").strip().lower() not in {"0", "false", "no", "off"}
        result = create_backup(
            BackupOptions(
                project_root=project_root,
                data_dir=data_dir,
                output_dir=output_dir,
                database_url=args.database_url,
                include_assets=include_assets and not args.no_assets,
                include_config=not args.no_config,
                skip_database=args.skip_database,
                note=args.note,
                retention_days=args.retention_days,
            )
        )
        print_result(result, args.as_json)
        return 0 if result.get("ok") else 1
    if args.command == "verify":
        result = verify_backup(Path(args.backup_file))
        print_result(result, args.as_json)
        return 0 if result.get("ok") else 1
    if args.command == "restore":
        result = restore_backup(
            RestoreOptions(
                backup_file=Path(args.backup_file),
                restore_data_dir=Path(args.restore_to),
                database_url=args.database_url,
                overwrite=args.overwrite,
                restore_database=not args.skip_database,
                restore_data_files=not args.skip_data_files,
            )
        )
        print_result(result, args.as_json)
        return 0 if result.get("ok") else 1
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
