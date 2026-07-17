from __future__ import annotations

import json
import os
import unittest

import requests

from services.chatgpt_service import ChatGPTService


AUTH_KEY = "chatgpt2api"
BASE_URL = "http://localhost:8000"


class ModelListTests(unittest.TestCase):
    def test_list_models_function(self):
        """????????????????????????"""

        class FakeBackend:
            def list_models(self):
                return {"object": "list", "data": [{"id": "auto"}, {"id": "gpt-image-2"}]}

        service = ChatGPTService(None)
        service._new_backend = lambda access_token=None: FakeBackend()  # type: ignore[assignment]

        result = service.list_models()

        self.assertEqual(result["object"], "list")
        self.assertIn("auto", [item["id"] for item in result["data"]])
        print("function result:")
        print(json.dumps(result, ensure_ascii=False, indent=2))

    @unittest.skipUnless(os.getenv("RUN_INTEGRATION_TESTS") == "1", "integration test: set RUN_INTEGRATION_TESTS=1 and start localhost service")
    def test_list_models_http(self):
        """测试通过 HTTP 接口获取模型列表。"""
        response = requests.get(
            f"{BASE_URL}/v1/models",
            headers={"Authorization": f"Bearer {AUTH_KEY}"},
            timeout=30,
        )
        print("http status:")
        print(response.status_code)
        print("http result:")
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))
