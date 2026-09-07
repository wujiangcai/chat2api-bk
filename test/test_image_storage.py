from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from services.image_asset_service import ImageAssetService
from services.image_storage import cleanup_expired_storage, delete_legacy_images
from services.storage.json_storage import JSONStorageBackend

ONE_PIXEL_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


class ImageStorageTests(unittest.TestCase):
    def test_delete_legacy_image_and_reject_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            images = Path(tmp_dir) / "images"
            nested = images / "2026" / "09" / "07"
            nested.mkdir(parents=True)
            target = nested / "keep.png"
            target.write_bytes(b"png-bytes")
            secret = Path(tmp_dir) / "secret.txt"
            secret.write_text("nope", encoding="utf-8")

            with patch("services.image_storage.config") as mock_config:
                mock_config.images_dir = images
                deleted = delete_legacy_images(["2026/09/07/keep.png"])
                traversal = delete_legacy_images(["../secret.txt"])

            self.assertEqual(deleted["removed_files"], 1)
            self.assertFalse(target.exists())
            self.assertEqual(traversal["removed_files"], 0)
            self.assertTrue(secret.exists())

    def test_asset_purge_removes_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            storage = JSONStorageBackend(base / "accounts.json")
            assets = ImageAssetService(storage, base / "assets")
            created = assets.archive_result(
                owner={"role": "admin"},
                result={"data": [{"b64_json": ONE_PIXEL_PNG_B64}]},
                source="unit-test",
                model="gpt-image-2",
                prompt="tiny",
            )
            self.assertEqual(len(created), 1)
            object_key = str(created[0].get("object_key") or "")
            self.assertTrue((base / "assets" / object_key).is_file())

            result = assets.purge_before(time.time() + 10, all_items=True)
            self.assertGreaterEqual(result["removed_files"], 1)
            self.assertEqual(assets.get_asset(str(created[0]["id"]))["status"], "deleted")
            self.assertFalse((base / "assets" / object_key).exists())

    def test_cleanup_expired_storage_clears_images_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            images = Path(tmp_dir) / "images"
            images.mkdir()
            stale = images / "old.png"
            stale.write_bytes(b"old")

            with patch("services.image_storage.config") as mock_config, patch(
                "services.image_storage.DATA_DIR", Path(tmp_dir)
            ):
                mock_config.images_dir = images
                mock_config.assets_dir = Path(tmp_dir) / "assets"
                mock_config.image_retention_days = 30
                result = cleanup_expired_storage(
                    older_than_days=0,
                    include_assets=False,
                    include_job_inputs=False,
                )

            self.assertEqual(result["removed_files"], 1)
            self.assertFalse(stale.exists())


if __name__ == "__main__":
    unittest.main()
