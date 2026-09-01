from __future__ import annotations

import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")

from services.account_service import AccountService
from services.chatgpt_service import ChatGPTService, ImageGenerationError
from services.config import config
from services.storage.json_storage import JSONStorageBackend


class ChatGPTServiceFailoverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.accounts_path = Path(self.tmp_dir.name) / "accounts.json"
        self.storage = JSONStorageBackend(self.accounts_path)
        self.account_service = AccountService(self.storage)
        self.account_service.add_accounts(["token-1", "token-2"])
        for token in ["token-1", "token-2"]:
            self.account_service.update_account(
                token,
                {
                    "status": "正常",
                    "type": "Free",
                    "quota": 1,
                    "image_quota_unknown": False,
                },
            )
        self.chatgpt_service = ChatGPTService(self.account_service)

        self.fetch_remote_patcher = patch.object(
            AccountService,
            "fetch_remote_info",
            side_effect=self._mock_fetch_remote_info,
        )
        self.fetch_remote_patcher.start()

        self.raw_image_bytes = b"fake-png-content"
        self.b64_image = base64.b64encode(self.raw_image_bytes).decode("ascii")

    def tearDown(self) -> None:
        self.fetch_remote_patcher.stop()
        self.tmp_dir.cleanup()

    def _mock_fetch_remote_info(self, access_token: str) -> dict:
        acc = self.account_service.get_account(access_token) or {}
        return {
            "email": f"{access_token}@example.com",
            "user_id": f"user-{access_token}",
            "type": acc.get("type", "Free"),
            "quota": acc.get("quota", 1),
            "image_quota_unknown": acc.get("image_quota_unknown", False),
            "limits_progress": [],
            "default_model_slug": "gpt-4o",
            "restore_at": None,
            "status": acc.get("status", "正常"),
        }

    def _create_backend_factory(self, fail_token: str = "token-1", fail_message: str = "Rate limit reached (429)"):
        def mock_new_backend(access_token: str = "") -> MagicMock:
            backend = MagicMock()
            if access_token == fail_token:
                backend.images_generations.side_effect = Exception(fail_message)
                backend.images_edits.side_effect = Exception(fail_message)
                backend.stream_image_chat_completions.side_effect = Exception(fail_message)
            else:
                backend.images_generations.return_value = {
                    "created": 1700000000,
                    "data": [{"b64_json": self.b64_image, "revised_prompt": "revised prompt"}],
                }
                backend.images_edits.return_value = {
                    "created": 1700000000,
                    "data": [{"b64_json": self.b64_image, "revised_prompt": "revised edit prompt"}],
                }
                backend.stream_image_chat_completions.return_value = iter([
                    {
                        "created": 1700000000,
                        "choices": [{
                            "delta": {"content": f"![image](data:image/png;base64,{self.b64_image})"},
                            "finish_reason": "stop",
                        }],
                    }
                ])
            return backend
        return mock_new_backend

    def test_text_to_image_failover_success(self) -> None:
        """场景 1（文生图）：配置 2 个 token，第 1 个 token 429 报错，第 2 个 token 成功返回。"""
        with patch.object(self.chatgpt_service, "_new_backend", side_effect=self._create_backend_factory("token-1")):
            result = self.chatgpt_service.generate_with_pool(
                prompt="draw a cat",
                model="gpt-image-1",
                n=1,
                response_format="b64_json",
            )

            self.assertIsNotNone(result)
            data = result.get("data")
            self.assertIsInstance(data, list)
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["b64_json"], self.b64_image)

            acc1 = self.account_service.get_account("token-1")
            self.assertIsNotNone(acc1)
            self.assertEqual(acc1["fail"], 1)
            self.assertEqual(acc1["consecutive_fail"], 1)

            acc2 = self.account_service.get_account("token-2")
            self.assertIsNotNone(acc2)
            self.assertEqual(acc2["success"], 1)
            self.assertEqual(acc2["consecutive_fail"], 0)

    def test_image_to_image_failover_success(self) -> None:
        """场景 2（图生图）：配置 2 个 token，第 1 个 token 调用 images_edits 报错，第 2 个 token 成功返回。"""
        with patch.object(self.chatgpt_service, "_new_backend", side_effect=self._create_backend_factory("token-1")):
            result = self.chatgpt_service.edit_with_pool(
                prompt="make it sunny",
                images=[(self.raw_image_bytes, "input.png", "image/png")],
                model="gpt-image-1",
                n=1,
                response_format="b64_json",
            )

            self.assertIsNotNone(result)
            data = result.get("data")
            self.assertIsInstance(data, list)
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["b64_json"], self.b64_image)

            acc1 = self.account_service.get_account("token-1")
            self.assertIsNotNone(acc1)
            self.assertEqual(acc1["fail"], 1)
            self.assertEqual(acc1["consecutive_fail"], 1)

            acc2 = self.account_service.get_account("token-2")
            self.assertIsNotNone(acc2)
            self.assertEqual(acc2["success"], 1)
            self.assertEqual(acc2["consecutive_fail"], 0)

    def test_stream_image_generation_failover_success(self) -> None:
        """场景 3（流式文生图）：第 1 个 token 异常，第 2 个 token 正常流式输出。"""
        with patch.object(self.chatgpt_service, "_new_backend", side_effect=self._create_backend_factory("token-1")):
            chunks = list(self.chatgpt_service.stream_image_generation(
                prompt="draw a sunset",
                model="gpt-image-1",
                n=1,
                response_format="b64_json",
            ))

            self.assertTrue(len(chunks) > 0)
            has_result_chunk = any(
                chunk.get("object") == "image.generation.result" and len(chunk.get("data", [])) > 0
                for chunk in chunks
            )
            self.assertTrue(has_result_chunk)

            acc1 = self.account_service.get_account("token-1")
            self.assertEqual(acc1["fail"], 1)
            self.assertEqual(acc1["consecutive_fail"], 1)

            acc2 = self.account_service.get_account("token-2")
            self.assertEqual(acc2["success"], 1)
            self.assertEqual(acc2["consecutive_fail"], 0)

    def test_stream_image_edit_failover_success(self) -> None:
        """场景 4（流式图生图）：第 1 个 token 异常，第 2 个 token 正常流式输出。"""
        with patch.object(self.chatgpt_service, "_new_backend", side_effect=self._create_backend_factory("token-1")):
            chunks = list(self.chatgpt_service.stream_image_edit(
                prompt="modify this landscape",
                images=[(self.raw_image_bytes, "input.png", "image/png")],
                model="gpt-image-1",
                n=1,
                response_format="b64_json",
            ))

            self.assertTrue(len(chunks) > 0)
            has_result_chunk = any(
                chunk.get("object") == "image.generation.result" and len(chunk.get("data", [])) > 0
                for chunk in chunks
            )
            self.assertTrue(has_result_chunk)

            acc1 = self.account_service.get_account("token-1")
            self.assertEqual(acc1["fail"], 1)
            self.assertEqual(acc1["consecutive_fail"], 1)

            acc2 = self.account_service.get_account("token-2")
            self.assertEqual(acc2["success"], 1)
            self.assertEqual(acc2["consecutive_fail"], 0)

    def test_all_tokens_fail_raises_image_generation_error(self) -> None:
        """场景 5：所有候选 token 均发生错误，尝试耗尽后抛出 ImageGenerationError。"""
        def mock_all_fail_backend(access_token: str = "") -> MagicMock:
            backend = MagicMock()
            backend.images_generations.side_effect = Exception("All accounts 429 rate limit")
            return backend

        with patch.object(self.chatgpt_service, "_new_backend", side_effect=mock_all_fail_backend):
            with self.assertRaises(ImageGenerationError):
                self.chatgpt_service.generate_with_pool(
                    prompt="draw a failing image",
                    model="gpt-image-1",
                    n=1,
                )

            acc1 = self.account_service.get_account("token-1")
            acc2 = self.account_service.get_account("token-2")
            self.assertEqual(acc1["fail"], 1)
            self.assertEqual(acc2["fail"], 1)

    def test_invalid_token_auto_removed_and_failover_succeeds(self) -> None:
        """场景 6：token-1 遇到 token_invalidated，自动移除该账号并换到 token-2 成功完成。"""
        with patch.object(type(config), "auto_remove_invalid_accounts", new_callable=PropertyMock, return_value=True):
            with patch.object(self.chatgpt_service, "_new_backend", side_effect=self._create_backend_factory("token-1", fail_message="token_invalidated")):
                result = self.chatgpt_service.generate_with_pool(
                    prompt="draw with invalid token retry",
                    model="gpt-image-1",
                    n=1,
                    response_format="b64_json",
                )

                self.assertIsNotNone(result)
                self.assertEqual(len(result["data"]), 1)
                self.assertNotIn("token-1", self.account_service.list_tokens())
                self.assertIn("token-2", self.account_service.list_tokens())
                acc2 = self.account_service.get_account("token-2")
                self.assertEqual(acc2["success"], 1)


    def test_stream_image_generation_fails_midstream_does_not_retry(self) -> None:
        """流式生成过程中若已输出 chunk 则不再换号重试，直接终止并报错。"""
        def mock_midstream_fail_backend(access_token: str = "") -> MagicMock:
            backend = MagicMock()
            def failing_stream(**kwargs):
                yield {
                    "created": 1700000000,
                    "upstream_event": {"type": "in_progress"},
                    "choices": [{"delta": {"content": "generating..."}}],
                }
                raise Exception("Stream connection dropped mid-stream")
            backend.stream_image_chat_completions.side_effect = failing_stream
            return backend

        with patch.object(self.chatgpt_service, "_new_backend", side_effect=mock_midstream_fail_backend):
            gen = self.chatgpt_service.stream_image_generation(
                prompt="draw a failing stream",
                model="gpt-image-1",
                n=1,
            )
            # 消费第 1 个 chunk
            first_chunk = next(gen)
            self.assertEqual(first_chunk.get("object"), "image.generation.chunk")
            # 下一个 chunk 抛出异常
            with self.assertRaises(ImageGenerationError):
                next(gen)

    def test_stream_image_edit_fails_midstream_does_not_retry(self) -> None:
        """流式编辑过程中若已输出 chunk 则不再换号重试，直接终止并报错。"""
        def mock_midstream_fail_backend(access_token: str = "") -> MagicMock:
            backend = MagicMock()
            def failing_stream(**kwargs):
                yield {
                    "created": 1700000000,
                    "upstream_event": {"type": "in_progress"},
                    "choices": [{"delta": {"content": "editing..."}}],
                }
                raise Exception("Stream connection dropped mid-stream")
            backend.stream_image_chat_completions.side_effect = failing_stream
            return backend

        with patch.object(self.chatgpt_service, "_new_backend", side_effect=mock_midstream_fail_backend):
            gen = self.chatgpt_service.stream_image_edit(
                prompt="edit a failing stream",
                images=[(self.raw_image_bytes, "input.png", "image/png")],
                model="gpt-image-1",
                n=1,
            )
            first_chunk = next(gen)
            self.assertEqual(first_chunk.get("object"), "image.generation.chunk")
            with self.assertRaises(ImageGenerationError):
                next(gen)

    def test_batch_generation_with_failover(self) -> None:
        """批量生成 n=2，第 1 张图片重试换号成功，第 2 张图片顺利完成。"""
        call_count = {"token-1": 0, "token-2": 0}
        def mock_backend(access_token: str = "") -> MagicMock:
            backend = MagicMock()
            def gen_img(**kwargs):
                call_count[access_token] += 1
                if access_token == "token-1" and call_count["token-1"] == 1:
                    raise Exception("Temporary 429")
                return {
                    "created": 1700000000,
                    "data": [{"b64_json": self.b64_image, "revised_prompt": "prompt"}],
                }
            backend.images_generations.side_effect = gen_img
            return backend

        # 给两个账号充足 quota
        for token in ["token-1", "token-2"]:
            self.account_service.update_account(token, {"quota": 5, "image_quota_unknown": True})

        with patch.object(self.chatgpt_service, "_new_backend", side_effect=mock_backend):
            result = self.chatgpt_service.generate_with_pool(
                prompt="draw two images",
                model="gpt-image-1",
                n=2,
                response_format="b64_json",
            )

            self.assertIsNotNone(result)
            self.assertEqual(len(result["data"]), 2)


if __name__ == "__main__":
    unittest.main()
