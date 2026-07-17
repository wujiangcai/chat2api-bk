import tempfile
import unittest
from pathlib import Path

from services.image_asset_service import ImageAssetService
from services.object_storage import LocalObjectStorage, S3ObjectStorage
from services.storage.json_storage import JSONStorageBackend

ONE_PIXEL_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


class FakeS3Client:
    def __init__(self):
        self.puts = []
        self.deletes = []

    def put_object(self, **kwargs):
        self.puts.append(kwargs)

    def delete_object(self, **kwargs):
        self.deletes.append(kwargs)


class ObjectStorageTests(unittest.TestCase):
    def test_local_object_storage_writes_and_deletes_file(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        root = Path(tmp_dir.name)
        storage = LocalObjectStorage(root)

        key = storage.put_object("2026/07/06/test.png", b"data", "image/png")
        self.assertEqual(key, "2026/07/06/test.png")
        self.assertEqual((root / key).read_bytes(), b"data")
        self.assertEqual(storage.public_url(key, base_url="https://img.example.com"), "https://img.example.com/assets/2026/07/06/test.png")

        storage.delete_object(key)
        self.assertFalse((root / key).exists())

    def test_s3_object_storage_uses_bucket_prefix_and_public_base_url(self):
        client = FakeS3Client()
        storage = S3ObjectStorage(
            bucket="images",
            endpoint_url="https://s3.example.com",
            public_base_url="https://cdn.example.com",
            key_prefix="prod",
            client=client,
        )

        key = storage.put_object("2026/07/06/test image.png", b"data", "image/png")
        self.assertEqual(key, "prod/2026/07/06/test image.png")
        self.assertEqual(client.puts[0]["Bucket"], "images")
        self.assertEqual(client.puts[0]["Key"], key)
        self.assertEqual(client.puts[0]["ContentType"], "image/png")
        self.assertEqual(storage.public_url(key), "https://cdn.example.com/prod/2026/07/06/test%20image.png")

        storage.delete_object(key)
        self.assertEqual(client.deletes[0], {"Bucket": "images", "Key": key})

    def test_image_asset_service_archives_to_s3_storage(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        base_dir = Path(tmp_dir.name)
        metadata_storage = JSONStorageBackend(base_dir / "accounts.json", base_dir / "auth_keys.json")
        fake_s3 = FakeS3Client()
        object_storage = S3ObjectStorage(
            bucket="images",
            public_base_url="https://cdn.example.com",
            key_prefix="gallery",
            client=fake_s3,
        )
        asset_service = ImageAssetService(metadata_storage, object_storage=object_storage)

        archived = asset_service.archive_result(
            owner={"role": "user", "user_id": "usr_1", "email": "user@example.com"},
            result={"data": [{"b64_json": ONE_PIXEL_PNG_B64}]},
            job_id="job_1",
            source="unit-test",
            model="gpt-image-2",
            prompt="tiny image",
        )

        self.assertEqual(len(archived), 1)
        self.assertEqual(len(fake_s3.puts), 1)
        self.assertTrue(fake_s3.puts[0]["Key"].startswith("gallery/"))
        self.assertEqual(archived[0]["storage_backend"], "s3")
        self.assertTrue(archived[0]["url"].startswith("https://cdn.example.com/gallery/"))


if __name__ == "__main__":
    unittest.main()
