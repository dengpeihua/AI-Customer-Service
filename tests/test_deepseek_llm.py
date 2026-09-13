from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.config import settings
from app.llm.deepseek import DeepSeekLLM


class DeepSeekLLMTests(unittest.TestCase):
    def setUp(self):
        self.previous = {
            name: getattr(settings, name)
            for name in (
                "deepseek_api_key",
                "deepseek_api_base",
                "deepseek_model",
                "deepseek_thinking",
                "minimax_api_key",
                "minimax_embedding_base_url",
                "minimax_embed_model",
            )
        }
        settings.deepseek_api_key = "test-deepseek-key"
        settings.deepseek_api_base = "https://api.deepseek.com"
        settings.deepseek_model = "deepseek-v4-flash"
        settings.deepseek_thinking = False
        settings.minimax_api_key = "test-embed-key"
        settings.minimax_embedding_base_url = "https://api.minimaxi.com/v1"
        settings.minimax_embed_model = "embo-01"

    def tearDown(self):
        for name, value in self.previous.items():
            setattr(settings, name, value)

    @patch("app.llm.deepseek.DefaultHttpxClient", return_value=Mock())
    @patch("app.llm.deepseek.OpenAI")
    @patch("app.llm.deepseek.MiniMaxEmbeddingClient")
    def test_routes_chat_to_deepseek_and_embeddings_to_minimax(
        self, embedding_client_cls, openai, _http
    ):
        chat_client = Mock()
        embed_client = embedding_client_cls.return_value
        openai.return_value = chat_client
        chat_client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="好的"))]
        )
        embed_client.embed.return_value = [[1.0], [2.0]]

        llm = DeepSeekLLM()
        self.assertEqual("好的", llm.chat("system", "user", temperature=0.2))
        self.assertEqual(
            [[1.0], [2.0]], llm.embed(["a", "b"], input_type="query")
        )

        self.assertEqual("deepseek-v4-flash", chat_client.chat.completions.create.call_args.kwargs["model"])
        self.assertEqual(
            {"thinking": {"type": "disabled"}},
            chat_client.chat.completions.create.call_args.kwargs["extra_body"],
        )
        embed_client.embed.assert_called_once_with(["a", "b"], input_type="query")
        self.assertEqual("https://api.deepseek.com", openai.call_args.kwargs["base_url"])

    def test_requires_both_chat_and_embedding_keys(self):
        settings.deepseek_api_key = ""
        with self.assertRaisesRegex(ValueError, "DEEPSEEK_API_KEY"):
            DeepSeekLLM()
        settings.deepseek_api_key = "present"
        settings.minimax_api_key = ""
        with self.assertRaisesRegex(ValueError, "MINIMAX_API_KEY"):
            DeepSeekLLM()


if __name__ == "__main__":
    unittest.main()
