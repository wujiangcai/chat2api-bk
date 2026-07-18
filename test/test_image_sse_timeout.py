from __future__ import annotations

import os
import queue
import threading
import unittest
from unittest.mock import patch

from curl_cffi.requests.models import STREAM_END

from services.openai_backend_api import OpenAIBackendAPI


class _StreamingResponse:
    def __init__(self, chunks: list[object] | None = None):
        self.queue: queue.Queue[object] = queue.Queue()
        for chunk in chunks or []:
            self.queue.put(chunk)
        self.quit_now = threading.Event()


class ImageSSETimeoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = OpenAIBackendAPI.__new__(OpenAIBackendAPI)

    def test_parse_image_sse_collects_ids_and_stops(self):
        response = _StreamingResponse([
            b'data: {"conversation_id":"conv-1","file":"file-abc","asset":"sediment://sed-1"}\n\n',
            STREAM_END,
        ])

        result = self.backend._parse_image_sse(response)  # type: ignore[arg-type]

        self.assertEqual(result["conversation_id"], "conv-1")
        self.assertEqual(result["file_ids"], ["file-abc"])
        self.assertEqual(result["sediment_ids"], ["sed-1"])
        self.assertTrue(response.quit_now.is_set())

    def test_parse_image_sse_times_out_and_cancels_stream(self):
        response = _StreamingResponse()

        with patch.dict(os.environ, {"IMAGE_SSE_TIMEOUT_SECONDS": "1"}):
            with self.assertRaisesRegex(TimeoutError, "image SSE timed out"):
                self.backend._parse_image_sse(response)  # type: ignore[arg-type]

        self.assertTrue(response.quit_now.is_set())


if __name__ == "__main__":
    unittest.main()
