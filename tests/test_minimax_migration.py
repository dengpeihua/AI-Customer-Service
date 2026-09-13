from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from app.config import settings
from app.llm.deepseek import DeepSeekLLM
from app.llm.minimax import MiniMaxEmbeddingClient


class MiniMaxMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
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
        settings.minimax_api_key = "test-minimax-key"
        settings.minimax_embedding_base_url = "https://api.minimaxi.com/v1"
        settings.minimax_embed_model = "embo-01"

    def tearDown(self) -> None:
        for name, value in self.previous.items():
            setattr(settings, name, value)

    @patch("app.llm.deepseek.DefaultHttpxClient", return_value=Mock())
    @patch("app.llm.deepseek.OpenAI")
    @patch("app.llm.deepseek.MiniMaxEmbeddingClient")
    def test_deepseek_chat_uses_minimax_embeddings(
        self, embedding_client_cls, openai, _http
    ) -> None:
        chat_client = Mock()
        embed_client = embedding_client_cls.return_value
        openai.return_value = chat_client
        chat_client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="好的"))]
        )
        embed_client.embed.return_value = [[1.0], [2.0]]

        llm = DeepSeekLLM()

        self.assertEqual("好的", llm.chat("system", "user", temperature=0.2))
        self.assertEqual([[1.0], [2.0]], llm.embed(["a", "b"], input_type="query"))
        embed_client.embed.assert_called_once_with(["a", "b"], input_type="query")
        self.assertEqual("https://api.deepseek.com", openai.call_args.kwargs["base_url"])

    def test_minimax_embedding_client_uses_native_request_and_response_contract(self) -> None:
        client = Mock()
        response = client.post.return_value
        response.json.return_value = {
            "base_resp": {"status_code": 0, "status_msg": ""},
            "vectors": [[1.0] * 1536, [2.0] * 1536],
        }

        vectors = MiniMaxEmbeddingClient(client=client).embed(
            ["a", "b"], input_type="query"
        )

        self.assertEqual(2, len(vectors))
        self.assertEqual([1.0, 1.0], vectors[0][:2])
        self.assertEqual([2.0, 2.0], vectors[1][:2])
        response.raise_for_status.assert_called_once_with()
        client.post.assert_called_once_with(
            "https://api.minimaxi.com/v1/embeddings",
            json={"model": "embo-01", "texts": ["a", "b"], "type": "query"},
        )

    def test_minimax_embedding_client_batches_with_requested_input_type(self) -> None:
        client = Mock()
        first_response = Mock()
        first_response.json.return_value = {
            "base_resp": {"status_code": 0},
            "vectors": [[1.0] * 1536] * 100,
        }
        second_response = Mock()
        second_response.json.return_value = {
            "base_resp": {"status_code": 0},
            "vectors": [[2.0] * 1536],
        }
        client.post.side_effect = [first_response, second_response]
        texts = [f"text-{index}" for index in range(101)]

        vectors = MiniMaxEmbeddingClient(client=client).embed(texts, input_type="db")

        self.assertEqual(101, len(vectors))
        self.assertEqual(
            [
                call(
                    "https://api.minimaxi.com/v1/embeddings",
                    json={"model": "embo-01", "texts": texts[:100], "type": "db"},
                ),
                call(
                    "https://api.minimaxi.com/v1/embeddings",
                    json={"model": "embo-01", "texts": texts[100:], "type": "db"},
                ),
            ],
            client.post.call_args_list,
        )

    def test_minimax_embedding_client_surfaces_api_error(self) -> None:
        client = Mock()
        client.post.return_value.json.return_value = {
            "base_resp": {"status_code": 2013, "status_msg": "invalid params"}
        }

        with self.assertRaisesRegex(RuntimeError, "2013.*invalid params"):
            MiniMaxEmbeddingClient(client=client).embed(["a"])

    def test_minimax_embedding_client_rejects_wrong_dimension(self) -> None:
        client = Mock()
        client.post.return_value.json.return_value = {
            "base_resp": {"status_code": 0},
            "vectors": [[1.0] * 1024],
        }

        with self.assertRaisesRegex(ValueError, "expected=1536 actual=1024"):
            MiniMaxEmbeddingClient(client=client).embed(["a"])

    def test_minimax_embedding_rejects_non_minimax_endpoint(self) -> None:
        settings.minimax_embedding_base_url = "https://example.invalid/v1"

        with self.assertRaisesRegex(ValueError, "MiniMax"):
            MiniMaxEmbeddingClient()

    def test_runtime_configuration_uses_requested_minimax_contract(self) -> None:
        root = Path(__file__).resolve().parents[1]
        expected = {
            "MEM0_EMBEDDING_PROVIDER": "minimax",
            "MINIMAX_EMBEDDING_BASE_URL": "https://api.minimaxi.com/v1",
            "MINIMAX_EMBED_MODEL": "embo-01",
            "EMBEDDING_DIMS": "1536",
            "COLLECTION_NAME": "memories_minimax_embo_01_1536",
        }
        for relative in (".env", ".env.example", "mem/.env", "mem/.env.example"):
            text = (root / relative).read_text(encoding="utf-8")
            values = dict(
                line.split("=", 1) for line in text.splitlines()
                if line and not line.startswith("#") and "=" in line
            )
            for name, value in expected.items():
                self.assertEqual(value, values.get(name), f"{relative}: {name}")


if __name__ == "__main__":
    unittest.main()
