"""Admin-facing image cache usage and cleanup.

Generated files currently land in two trees:

- ``data/images`` — legacy URL results from ``_save_image_bytes``
- ``data/assets`` — gallery objects archived by ``ImageAssetService``

Startup retention used to scan only ``images/``, so ``assets/`` grew without a
UI delete path. This module is the single cleanup entry for both trees plus
stale ``job-inputs``.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

from services.config import DATA_DIR, config


def _dir_stats(path: Path) -> dict[str, int]:
    files = 0
    nbytes = 0
    if path.exists():
        for item in path.rglob("*"):
            if item.is_file():
                files += 1
                nbytes += item.stat().st_size
    return {"files": files, "bytes": nbytes}


def storage_usage() -> dict[str, Any]:
    images = _dir_stats(config.images_dir)
    assets = _dir_stats(config.assets_dir)
    job_inputs = _dir_stats(DATA_DIR / "job-inputs")
    jobs_path = DATA_DIR / "image_jobs.json"
    jobs_bytes = jobs_path.stat().st_size if jobs_path.is_file() else 0
    return {
        "images": images,
        "assets": assets,
        "job_inputs": job_inputs,
        "jobs_bytes": jobs_bytes,
        "total_bytes": images["bytes"] + assets["bytes"] + job_inputs["bytes"] + jobs_bytes,
        "retention_days": config.image_retention_days,
    }


def _safe_under(root: Path, relative: str) -> Path:
    clean = relative.replace("\\", "/").lstrip("/")
    if not clean or ".." in clean.split("/"):
        raise ValueError("path is invalid")
    resolved_root = root.resolve()
    candidate = (resolved_root / clean).resolve()
    candidate.relative_to(resolved_root)
    return candidate


def _prune_empty_dirs(root: Path) -> None:
    if not root.exists():
        return
    for path in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            path.rmdir()
        except OSError:
            pass


def _delete_files_older_than(root: Path, cutoff_epoch: float) -> dict[str, int]:
    removed_files = 0
    freed_bytes = 0
    if root.exists():
        for path in list(root.rglob("*")):
            if not path.is_file():
                continue
            if path.stat().st_mtime > cutoff_epoch:
                continue
            size = path.stat().st_size
            path.unlink(missing_ok=True)
            removed_files += 1
            freed_bytes += size
    _prune_empty_dirs(root)
    return {"removed_files": removed_files, "freed_bytes": freed_bytes}


def delete_legacy_images(relative_paths: list[str]) -> dict[str, Any]:
    removed_files = 0
    freed_bytes = 0
    errors: list[str] = []
    root = config.images_dir
    for raw in relative_paths:
        rel = str(raw or "").strip()
        if not rel:
            continue
        try:
            path = _safe_under(root, rel)
        except ValueError:
            errors.append(rel)
            continue
        if not path.is_file():
            errors.append(rel)
            continue
        size = path.stat().st_size
        path.unlink()
        removed_files += 1
        freed_bytes += size
    _prune_empty_dirs(root)
    return {"removed_files": removed_files, "freed_bytes": freed_bytes, "errors": errors}


def cleanup_expired_storage(
    *,
    older_than_days: int | None = None,
    include_images: bool = True,
    include_assets: bool = True,
    include_job_inputs: bool = True,
) -> dict[str, Any]:
    days = config.image_retention_days if older_than_days is None else max(0, int(older_than_days))
    cutoff_epoch = time.time() + 1 if days == 0 else time.time() - days * 86400
    removed_files = 0
    freed_bytes = 0
    marked_deleted = 0

    if include_images:
        result = _delete_files_older_than(config.images_dir, cutoff_epoch)
        removed_files += result["removed_files"]
        freed_bytes += result["freed_bytes"]

    if include_assets:
        from services.image_asset_service import image_asset_service

        result = image_asset_service.purge_before(cutoff_epoch, all_items=days == 0)
        removed_files += int(result.get("removed_files") or 0)
        freed_bytes += int(result.get("freed_bytes") or 0)
        marked_deleted += int(result.get("marked_deleted") or 0)
        orphan = _delete_files_older_than(config.assets_dir, cutoff_epoch)
        removed_files += orphan["removed_files"]
        freed_bytes += orphan["freed_bytes"]

    if include_job_inputs:
        result = _delete_files_older_than(DATA_DIR / "job-inputs", cutoff_epoch)
        removed_files += result["removed_files"]
        freed_bytes += result["freed_bytes"]

    return {
        "older_than_days": days,
        "removed_files": removed_files,
        "freed_bytes": freed_bytes,
        "marked_deleted": marked_deleted,
        "usage": storage_usage(),
    }
