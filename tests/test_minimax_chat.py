from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.config import Settings, settings, validate_agent_routing_config, validate_production_secrets
from app.dialog.agent_orchestrator import _run_with_configured_model
from app.llm import close_llm, get_llm, runtime_summary
from app.llm.minimax_chat import MiniMaxLLM, minimax_chat_config
from app.ops.service import _model_gateway


class MiniMaxChatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original = {
            name: getattr(settings, name)
            for name in (
                "llm_provider", "minimax_api_key", "minimax_api_base", "minimax_model",
                "deepseek_api_key", "minimax_embedding_base_url", "minimax_embed_model",
            )
        }
        settings.llm_provider = "minimax"
        settings.minimax_api_key = "test-minimax-key"
        settings.minimax_api_base = "https://api.minimaxi.com/v1"
        settings.minimax_model = "MiniMax-M3"
        settings.deepseek_api_key = ""
        settings.minimax_embedding_base_url = "https://api.minimaxi.com/v1"
        settings.minimax_embed_model = "embo-01"
        close_llm()

    def tearDown(self) -> None:
        close_llm()
        for name, value in self.original.items():
            setattr(settings, name, value)

    @patch("app.llm.minimax_chat.DefaultHttpxClient", return_value=Mock())
    @patch("app.llm.minimax_chat.OpenAI")
    @patch("app.llm.minimax_chat.MiniMaxEmbeddingClient")
    def test_chat_and_embedding_use_minimax_without_deepseek_key(
        self, embedding_cls: Mock, openai_cls: Mock, http_cls: Mock,
    ) -> None:
        client = openai_cls.return_value
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content="好的", reasoning_content="internal reasoning",
            ))]
        )
        embedding_cls.return_value.embed.return_value = [[1.0] * 1536]
        llm = get_llm()
        self.assertIsInstance(llm, MiniMaxLLM)
        self.assertIs(llm, get_llm())
        self.assertEqual("好的", llm.chat("system", "user", temperature=0.0))
        self.assertEqual(1536, len(llm.embed(["hello"], input_type="query")[0]))
        embedding_cls.return_value.embed.assert_called_once_with(["hello"], input_type="query")
        self.assertEqual("test-minimax-key", openai_cls.call_args.kwargs["api_key"])
        self.assertEqual("https://api.minimaxi.com/v1", openai_cls.call_args.kwargs["base_url"])
        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual("MiniMax-M3", request["model"])
        self.assertEqual(0.0, request["temperature"])
        self.assertEqual({"thinking": {"type": "disabled"}, "reasoning_split": True}, request["extra_body"])
        http_cls.assert_called_once_with(trust_env=False)
        close_llm()
        client.close.assert_called_once_with()
        embedding_cls.return_value.close.assert_called_once_with()

    @patch("app.llm.minimax_chat.DefaultHttpxClient", return_value=Mock())
    @patch("app.llm.minimax_chat.OpenAI")
    @patch("app.llm.minimax_chat.MiniMaxEmbeddingClient")
    def test_empty_reply_is_an_error(self, embedding_cls: Mock, openai_cls: Mock, http_cls: Mock) -> None:
        client = openai_cls.return_value
        client.chat.completions.create.return_value = SimpleNamespace(choices=[])
        llm = get_llm()
        with self.assertRaisesRegex(RuntimeError, "空回复"):
            llm.chat("system", "user")

    def test_missing_key_and_untrusted_endpoints_are_rejected(self) -> None:
        settings.minimax_api_key = ""
        with self.assertRaisesRegex(ValueError, "MINIMAX_API_KEY"):
            minimax_chat_config()
        settings.minimax_api_key = "test-key"
        for url in (
            "http://api.minimaxi.com/v1", "https://example.invalid/v1",
            "https://api.minimaxi.com.example.invalid/v1", "https://api.minimaxi.com/v1?redirect=x",
            "https://user@api.minimaxi.com/v1", "https://api.minimaxi.com:8080/v1",
        ):
            with self.subTest(url=url):
                settings.minimax_api_base = url
                with self.assertRaisesRegex(ValueError, "MINIMAX_API_BASE"):
                    minimax_chat_config()

    def test_validation_and_status_only_require_minimax_key(self) -> None:
        validate_agent_routing_config(Settings(
            llm_provider="minimax", minimax_api_key="test-key", deepseek_api_key="",
        ))
        with self.assertRaisesRegex(RuntimeError, "minimax_api_key"):
            validate_agent_routing_config(Settings(llm_provider="minimax", minimax_api_key=""))
        with self.assertRaisesRegex(RuntimeError, "minimax_api_key"):
            validate_production_secrets(Settings(
                app_env="prod", llm_provider="minimax", minimax_api_key="",
                jwt_secret="strong-test-secret-value", bootstrap_token="strong-test-bootstrap-value",
                field_enc_key="test-key",
            ))
        gateway = _model_gateway()
        self.assertTrue(gateway["configured"])
        self.assertEqual("MiniMax-M3", gateway["chat_model"])
        self.assertEqual("embo-01", gateway["embedding_model"])
        self.assertIn("provider=minimax chat_model=MiniMax-M3", runtime_summary())
        self.assertNotIn(settings.minimax_api_key, runtime_summary())

    def test_agent_client_uses_same_minimax_model_key_and_parameters(self) -> None:
        client = Mock(close=AsyncMock())
        with (
            patch("app.dialog.agent_orchestrator.AsyncOpenAI", return_value=client) as client_cls,
            patch("app.dialog.agent_orchestrator.DefaultAsyncHttpxClient", return_value=Mock()),
            patch("app.dialog.agent_orchestrator.OpenAIChatCompletionsModel") as model_cls,
            patch("app.dialog.agent_orchestrator._run_with_model", new_callable=AsyncMock) as run,
        ):
            context = Mock()
            items = [{"role": "user", "content": "你好"}]
            result = asyncio.run(_run_with_configured_model(context, items))
            self.assertIs(result, run.return_value)
            self.assertEqual("test-minimax-key", client_cls.call_args.kwargs["api_key"])
            self.assertEqual("https://api.minimaxi.com/v1", client_cls.call_args.kwargs["base_url"])
            model_cls.assert_called_once_with(model="MiniMax-M3", openai_client=client)
            self.assertEqual(minimax_chat_config()[2], run.call_args.kwargs["extra_body"])
            client.close.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
