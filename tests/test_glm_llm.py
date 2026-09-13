from __future__ import annotations

import asyncio
import json
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from dotenv import dotenv_values
from openai import AsyncOpenAI

from app.config import Settings, settings, validate_agent_routing_config, validate_production_secrets
from app.dialog.agent_orchestrator import (
    AgentRunContext, _run_with_configured_model, build_customer_service_agents,
)
from app.llm import close_llm, get_llm, runtime_summary
from app.llm.glm import GLMLLM, glm_chat_config
from app.ops.service import _model_gateway


class GLMLLMTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in {
            "llm_provider": "glm", "glm_api_key": "test-glm-key",
            "glm_api_base": "https://open.bigmodel.cn/api/paas/v4",
            "glm_model": "glm-4.7", "glm_embed_model": "embedding-3",
            "llm_embedding_dimension": 2048, "minimax_api_key": "", "deepseek_api_key": "",
        }.items():
            self.stack.enter_context(patch.object(settings, name, value))
        self.stack.enter_context(patch("app.llm.glm.EMBED_DIM", 2048))
        self.stack.enter_context(patch("app.llm.EMBED_DIM", 2048))
        self.stack.enter_context(patch("app.ops.service.EMBED_DIM", 2048))
        self.openai_cls = self.stack.enter_context(patch("app.llm.glm.OpenAI"))
        self.stack.enter_context(patch("app.llm.glm.DefaultHttpxClient", return_value=Mock()))
        self.client = self.openai_cls.return_value
        close_llm()
        self.addCleanup(close_llm)

    def test_chat_selects_glm_key_model_and_returns_only_content(self) -> None:
        self.client.chat.completions.create.return_value = SimpleNamespace(choices=[
            SimpleNamespace(message=SimpleNamespace(content="你好", reasoning_content="private reasoning"))
        ])
        llm = get_llm()
        self.assertIsInstance(llm, GLMLLM)
        self.assertIs(llm, get_llm())
        self.assertEqual("你好", llm.chat("system", "user", temperature=0.0))
        self.assertEqual("test-glm-key", self.openai_cls.call_args.kwargs["api_key"])
        self.assertEqual("https://open.bigmodel.cn/api/paas/v4", self.openai_cls.call_args.kwargs["base_url"])
        request = self.client.chat.completions.create.call_args.kwargs
        self.assertEqual("glm-4.7", request["model"])
        self.assertEqual({"thinking": {"type": "disabled"}}, request["extra_body"])
        self.assertEqual(0.01, request["temperature"])
        close_llm()
        self.client.close.assert_called_once_with()

    def test_embedding_batches_64_and_restores_input_order(self) -> None:
        def embed_response(**kwargs: object) -> SimpleNamespace:
            batch = kwargs["input"]
            return SimpleNamespace(data=[
                SimpleNamespace(index=index, embedding=[float(text)] * 2048)
                for index, text in reversed(list(enumerate(batch)))
            ])

        self.client.embeddings.create.side_effect = embed_response
        texts = [str(index) for index in range(65)]
        vectors = get_llm().embed(texts, input_type="query")
        self.assertEqual(list(map(float, texts)), [vector[0] for vector in vectors])
        calls = self.client.embeddings.create.call_args_list
        self.assertEqual([64, 1], [len(call.kwargs["input"]) for call in calls])
        for call in calls:
            self.assertEqual("embedding-3", call.kwargs["model"])
            self.assertEqual(2048, call.kwargs["dimensions"])
            self.assertEqual("float", call.kwargs["encoding_format"])
            self.assertNotIn("type", call.kwargs)

    def test_embedding_rejects_malformed_responses(self) -> None:
        for items, expected in (
            ([], "indices"),
            ([SimpleNamespace(index=1, embedding=[0.0] * 2048)], "indices"),
            ([SimpleNamespace(index=0, embedding=[0.0] * 1536)], "dimension"),
            ([SimpleNamespace(index=0, embedding=[float("nan")] * 2048)], "non-finite"),
        ):
            with self.subTest(expected=expected):
                self.client.embeddings.create.return_value = SimpleNamespace(data=items)
                with self.assertRaisesRegex(ValueError, expected):
                    get_llm().embed(["hello"])

    def test_empty_inputs_and_api_failure_do_not_fall_back(self) -> None:
        llm = get_llm()
        self.assertEqual([], llm.embed([]))
        self.client.embeddings.create.assert_not_called()
        self.client.chat.completions.create.return_value = SimpleNamespace(choices=[])
        with self.assertRaisesRegex(RuntimeError, "空回复"):
            llm.chat("system", "hello")
        self.client.chat.completions.create.side_effect = httpx.ReadTimeout("test timeout")
        with self.assertRaises(httpx.ReadTimeout):
            llm.chat("system", "hello")

    def test_rejects_missing_key_untrusted_endpoint_and_minimax_dimension(self) -> None:
        with patch.object(settings, "glm_api_key", ""):
            with self.assertRaisesRegex(ValueError, "GLM_API_KEY"):
                get_llm()
        for url in (
            "http://open.bigmodel.cn/api/paas/v4", "https://example.invalid/api/paas/v4",
            "https://open.bigmodel.cn/api/coding/paas/v4",
            "https://user@open.bigmodel.cn/api/paas/v4",
            "https://open.bigmodel.cn/api/paas/v4?redirect=other",
        ):
            with self.subTest(url=url), patch.object(settings, "glm_api_base", url):
                with self.assertRaisesRegex(ValueError, "GLM_API_BASE"):
                    glm_chat_config()
        with patch("app.llm.glm.EMBED_DIM", 1536):
            with self.assertRaisesRegex(ValueError, "LLM_EMBEDDING_DIMENSION"):
                GLMLLM()
        self.openai_cls.assert_not_called()

    def test_validation_and_status_use_glm_without_other_provider_keys(self) -> None:
        validate_agent_routing_config(Settings(
            llm_provider="glm", glm_api_key="test-key", llm_embedding_dimension=2048,
            minimax_api_key="", deepseek_api_key="",
        ))
        for values, error in (
            ({"glm_api_key": "", "llm_embedding_dimension": 2048}, "glm_api_key"),
            ({"glm_api_key": "test-key", "llm_embedding_dimension": 1536}, "重建知识库"),
        ):
            with self.subTest(error=error), self.assertRaisesRegex(RuntimeError, error):
                validate_agent_routing_config(Settings(llm_provider="glm", **values))
        with self.assertRaisesRegex(RuntimeError, "glm_api_key"):
            validate_production_secrets(Settings(app_env="prod", llm_provider="glm", glm_api_key=""))
        gateway = _model_gateway()
        self.assertTrue(gateway["configured"])
        self.assertEqual("glm-4.7", gateway["chat_model"])
        self.assertEqual("embedding-3", gateway["embedding_model"])
        self.assertEqual(2048, gateway["embedding_dimension"])
        self.assertIn("provider=glm chat_model=glm-4.7 embed_model=embedding-3 embed_dim=2048", runtime_summary())
        self.assertNotIn("test-glm-key", runtime_summary())

    def test_agent_handoff_uses_glm_compatible_requests_without_network(self) -> None:
        requests: list[dict] = []

        def handle(request: httpx.Request) -> httpx.Response:
            self.assertEqual("Bearer test-glm-key", request.headers["authorization"])
            self.assertEqual("open.bigmodel.cn", request.url.host)
            body = json.loads(request.content)
            requests.append(body)
            self.assertEqual("glm-4.7", body["model"])
            self.assertEqual("auto", body["tool_choice"])
            self.assertNotIn("parallel_tool_calls", body)
            self.assertNotIn("reasoning_split", body)
            self.assertEqual({"type": "disabled"}, body["thinking"])
            self.assertGreater(body["temperature"], 0)
            if len(requests) == 1:
                message = {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "call_test", "type": "function", "function": {
                        "name": "transfer_to_chitchat_agent",
                        "arguments": '{"reason":"问候","summary":"客户打招呼"}',
                    },
                }]}
                finish_reason = "tool_calls"
            else:
                message = {"role": "assistant", "content": "你好，很高兴和你聊天。"}
                finish_reason = "stop"
            return httpx.Response(200, json={
                "id": "chat-test", "object": "chat.completion", "created": 0,
                "model": "glm-4.7", "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            })

        async def run() -> object:
            client = AsyncOpenAI(
                api_key="test-glm-key", base_url=settings.glm_api_base,
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
            )
            context = AgentRunContext(
                db=Mock(), retrieval_llm=Mock(), tenant_id=0, channel="test", contact_id="test",
                query="你好", tone_instruction="简洁", temperature=0.2, persona="", memory_note="",
                handoff_reply="转接人工", kb_loaded=True,
            )
            with (
                patch("app.dialog.agent_orchestrator.AsyncOpenAI", return_value=client) as client_cls,
                patch("app.dialog.agent_orchestrator.DefaultAsyncHttpxClient", return_value=Mock()),
            ):
                result = await _run_with_configured_model(context, [{"role": "user", "content": "你好"}])
                self.assertEqual("test-glm-key", client_cls.call_args.kwargs["api_key"])
                return result

        result = asyncio.run(run())
        self.assertEqual("chitchat", result.route)
        self.assertEqual(2, len(requests))
        self.assertEqual("你好，很高兴和你聊天。", result.reply_text)


class GLMInactiveConfigurationTests(unittest.TestCase):
    def test_local_glm_key_is_commented_and_minimax_remains_active(self) -> None:
        root = Path(__file__).resolve().parents[1]
        values = dotenv_values(root / ".env")
        self.assertEqual("minimax", values.get("LLM_PROVIDER"))
        self.assertEqual("1536", values.get("LLM_EMBEDDING_DIMENSION"))
        self.assertFalse(bool(values.get("GLM_API_KEY")), "GLM key must remain inactive")
        self.assertTrue(any(
            line.startswith("# GLM_API_KEY=")
            for line in (root / ".env").read_text(encoding="utf-8").splitlines()
        ))
        workflow = build_customer_service_agents(model="test-model")
        self.assertEqual("required", workflow.triage.model_settings.tool_choice)
        with patch("app.llm.glm.GLMLLM") as glm_cls:
            close_llm()
            try:
                with patch("app.llm.minimax_chat.MiniMaxLLM"):
                    get_llm()
                glm_cls.assert_not_called()
            finally:
                close_llm()


if __name__ == "__main__":
    unittest.main()
