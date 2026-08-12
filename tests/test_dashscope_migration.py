from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

from app.config import settings
from app.llm.dashscope import DashScopeEmbeddingClient
from app.llm.deepseek import DeepSeekLLM


class DashScopeMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous = {
            name: getattr(settings, name)
            for name in (
                "deepseek_api_key",
                "deepseek_api_base",
                "deepseek_model",
                "deepseek_thinking",
                "dashscope_api_key",
                "dashscope_base_url",
                "llm_embed_model",
            )
        }
        settings.deepseek_api_key = "test-deepseek-key"
        settings.deepseek_api_base = "https://api.deepseek.test"
        settings.deepseek_model = "deepseek-v4-flash"
        settings.deepseek_thinking = False
        settings.dashscope_api_key = "test-dashscope-key"
        settings.dashscope_base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
        settings.llm_embed_model = "text-embedding-v3"

    def tearDown(self) -> None:
        for name, value in self.previous.items():
            setattr(settings, name, value)

    @patch("app.llm.deepseek.DefaultHttpxClient", return_value=Mock())
    @patch("app.llm.deepseek.OpenAI")
    @patch("app.llm.deepseek.DashScopeEmbeddingClient")
    def test_deepseek_chat_uses_dashscope_embeddings(
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
        self.assertEqual("https://api.deepseek.test", openai.call_args.kwargs["base_url"])

    @patch("app.llm.dashscope.DefaultHttpxClient", return_value=Mock())
    @patch("app.llm.dashscope.OpenAI")
    def test_dashscope_embedding_client_preserves_response_order(
        self, openai, _http
    ) -> None:
        client = openai.return_value
        client.embeddings.create.return_value = SimpleNamespace(data=[
            SimpleNamespace(index=1, embedding=[2.0]),
            SimpleNamespace(index=0, embedding=[1.0]),
        ])

        vectors = DashScopeEmbeddingClient().embed(["a", "b"], input_type="query")

        self.assertEqual([[1.0], [2.0]], vectors)
        client.embeddings.create.assert_called_once_with(
            model="text-embedding-v3", input=["a", "b"]
        )

    def test_dashscope_embedding_rejects_non_bailian_endpoint(self) -> None:
        settings.dashscope_base_url = "https://example.invalid/v1"

        with self.assertRaisesRegex(ValueError, "阿里云百炼"):
            DashScopeEmbeddingClient()

    def test_project_runtime_has_no_removed_embedding_provider(self) -> None:
        root = Path(__file__).resolve().parents[1]
        removed_provider = "mini" + "max"
        paths = [
            root / "app" / "config.py",
            root / "app" / "llm" / "__init__.py",
            root / "app" / "llm" / "deepseek.py",
            root / "app" / "ops" / "service.py",
            root / "scripts" / "Restart-AIRuntime.ps1",
            root / ".env",
            root / ".env.example",
            root / "mem" / ".env",
            root / "mem" / ".env.example",
            root / "mem" / "server" / "main.py",
            root / "README.md",
        ]
        paths.extend((root / "mem" / "mem").rglob("*.py"))
        offenders = [
            str(path.relative_to(root))
            for path in paths
            if removed_provider in path.read_text(encoding="utf-8").lower()
        ]

        self.assertEqual([], offenders)
        self.assertFalse((root / "app" / "llm" / f"{removed_provider}.py").exists())
        self.assertFalse((root / "mem" / "mem" / "llms" / f"{removed_provider}.py").exists())
        self.assertFalse(
            (root / "mem" / "mem" / "configs" / "llms" / f"{removed_provider}.py").exists()
        )
        env_text = (root / ".env").read_text(encoding="utf-8")
        self.assertIn("LLM_EMBEDDING_DIMENSION=1024", env_text)


if __name__ == "__main__":
    unittest.main()
