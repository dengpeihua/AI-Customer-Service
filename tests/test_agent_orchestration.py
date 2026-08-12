from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.dialog.agent_orchestrator import (
    AFTER_SALES_ROUTE,
    CHITCHAT_ROUTE,
    HUMAN_ROUTE,
    AgentRunContext,
    build_agent_input,
    build_customer_service_agents,
    run_customer_service_agents,
)


class AgentWorkflowDefinitionTests(unittest.TestCase):
    def test_triage_exposes_exactly_three_structured_handoffs(self) -> None:
        workflow = build_customer_service_agents(model="test-model")

        handoffs = {item.tool_name: item for item in workflow.triage.handoffs}

        self.assertEqual(
            {
                "transfer_to_after_sales_agent",
                "transfer_to_chitchat_agent",
                "transfer_to_human_agent",
            },
            set(handoffs),
        )
        for item in handoffs.values():
            self.assertEqual({"reason", "summary"}, set(item.input_json_schema["required"]))
            self.assertIsNotNone(item.input_filter)

class AgentInputTests(unittest.TestCase):
    def test_history_is_limited_and_handoff_notices_are_not_replayed(self) -> None:
        messages = [
            SimpleNamespace(direction="in", provenance="customer", text="旧问题"),
            SimpleNamespace(direction="out", provenance="ai", text="旧回答"),
            SimpleNamespace(direction="out", provenance="handoff", text="正在转人工"),
            SimpleNamespace(direction="in", provenance="customer", text="最近问题"),
            SimpleNamespace(direction="out", provenance="human", text="人工回复"),
        ]

        items = build_agent_input(messages, "本轮问题", history_limit=3)

        self.assertEqual(
            [
                {"role": "assistant", "content": "旧回答"},
                {"role": "user", "content": "最近问题"},
                {"role": "assistant", "content": "人工回复"},
                {"role": "user", "content": "本轮问题"},
            ],
            items,
        )

    def test_handoff_customer_batch_and_notice_are_archived_together(self) -> None:
        messages = [
            SimpleNamespace(direction="in", sender="customer", provenance="customer",
                            text="产品现在的价钱是怎样的"),
            SimpleNamespace(direction="out", sender="agent", provenance="handoff",
                            text="帮您转接人工客服回复中，请稍等"),
            SimpleNamespace(direction="in", sender="customer", provenance="customer",
                            text="我和我弟感情挺好的"),
        ]

        items = build_agent_input(
            messages,
            "我有一只小狗叫麻将，他是一只很乖的小狗",
            history_limit=12,
        )

        serialized = "\n".join(item["content"] for item in items)
        self.assertNotIn("产品现在的价钱", serialized)
        self.assertNotIn("转接人工", serialized)
        self.assertIn("我和我弟感情挺好的", items[-1]["content"])
        self.assertIn("我有一只小狗叫麻将", items[-1]["content"])

    def test_consecutive_unanswered_customer_messages_form_one_current_batch(self) -> None:
        messages = [
            SimpleNamespace(direction="in", sender="customer", provenance="customer",
                            text="我和我弟感情挺好的"),
            SimpleNamespace(direction="in", sender="customer", provenance="customer",
                            text="我们经常一起打游戏"),
        ]

        items = build_agent_input(
            messages,
            "我有一只小狗叫麻将",
            history_limit=12,
        )

        self.assertEqual(1, len(items))
        self.assertEqual("user", items[0]["role"])
        self.assertIn("我和我弟感情挺好的", items[0]["content"])
        self.assertIn("我们经常一起打游戏", items[0]["content"])
        self.assertIn("我有一只小狗叫麻将", items[0]["content"])

    def test_resolved_history_is_kept_before_current_customer_batch(self) -> None:
        messages = [
            SimpleNamespace(direction="in", sender="customer", provenance="customer",
                            text="你们几点营业"),
            SimpleNamespace(direction="out", sender="ai", provenance="ai",
                            text="每天九点到二十一点营业"),
            SimpleNamespace(direction="in", sender="customer", provenance="customer",
                            text="我今天心情不错"),
            SimpleNamespace(direction="in", sender="customer", provenance="customer",
                            text="刚和弟弟打完游戏"),
        ]

        items = build_agent_input(messages, "麻将也在旁边", history_limit=12)

        self.assertEqual(
            [
                {"role": "user", "content": "你们几点营业"},
                {"role": "assistant", "content": "每天九点到二十一点营业"},
            ],
            items[:2],
        )
        self.assertEqual("user", items[-1]["role"])
        self.assertIn("我今天心情不错", items[-1]["content"])
        self.assertIn("刚和弟弟打完游戏", items[-1]["content"])
        self.assertIn("麻将也在旁边", items[-1]["content"])


class AgentRunnerTests(unittest.TestCase):
    def _context(self) -> AgentRunContext:
        return AgentRunContext(
            db=MagicMock(),
            retrieval_llm=MagicMock(),
            tenant_id=7,
            channel="wechat_personal",
            contact_id="wxid_friend",
            query="多久发货？",
            tone_instruction="温暖、简洁",
            temperature=0.2,
            persona="",
            memory_note="",
            handoff_reply="帮您转接人工客服回复中，请稍等",
        )

    def test_rag_handoff_retrieves_and_keeps_only_qualified_hits(self) -> None:
        context = self._context()

        async def fake_run(starting_agent, *, input, context, **_kwargs):
            del input
            target = next(
                item for item in starting_agent.handoffs
                if item.tool_name == "transfer_to_after_sales_agent"
            )
            agent = await target.on_invoke_handoff(
                SimpleNamespace(context=context),
                '{"reason":"需要查询发货规则","summary":"客户询问发货时效"}',
            )
            return SimpleNamespace(last_agent=agent, final_output="付款后 48 小时内发货。")

        with (
            patch(
                "app.dialog.agent_orchestrator.retrieve",
                return_value=[
                    {"text": "付款后 48 小时内发货。", "distance": 0.1},
                    {"text": "不相关内容", "distance": 0.8},
                ],
            ) as retrieve_mock,
            patch("app.dialog.agent_orchestrator.Runner.run", new=AsyncMock(side_effect=fake_run)),
        ):
            result = run_customer_service_agents(
                context,
                [{"role": "user", "content": context.query}],
                model="test-model",
            )

        self.assertEqual(AFTER_SALES_ROUTE, result.route)
        self.assertEqual("付款后 48 小时内发货。", result.reply_text)
        self.assertEqual([{"text": "付款后 48 小时内发货。", "distance": 0.1}], result.kb_hits)
        self.assertEqual("需要查询发货规则", result.reason)
        retrieve_mock.assert_called_once_with(
            context.db, context.retrieval_llm, 7, "多久发货？", 4,
        )

    def test_chitchat_and_human_routes_are_resolved_from_last_agent(self) -> None:
        for tool_name, expected_route, final_output in (
            ("transfer_to_chitchat_agent", CHITCHAT_ROUTE, "我也很喜欢小狗。"),
            ("transfer_to_human_agent", HUMAN_ROUTE, "已记录转人工请求。"),
        ):
            with self.subTest(tool_name=tool_name):
                context = self._context()

                async def fake_run(starting_agent, *, input, context, **_kwargs):
                    del input
                    target = next(
                        item for item in starting_agent.handoffs if item.tool_name == tool_name
                    )
                    agent = await target.on_invoke_handoff(
                        SimpleNamespace(context=context),
                        '{"reason":"测试路由","summary":"测试摘要"}',
                    )
                    return SimpleNamespace(last_agent=agent, final_output=final_output)

                with patch(
                    "app.dialog.agent_orchestrator.Runner.run",
                    new=AsyncMock(side_effect=fake_run),
                ):
                    result = run_customer_service_agents(
                        context,
                        [{"role": "user", "content": context.query}],
                        model="test-model",
                    )

                self.assertEqual(expected_route, result.route)
                self.assertEqual("测试路由", result.reason)

    def test_legacy_route_hints_cannot_bypass_triage(self) -> None:
        for legacy_hint in (AFTER_SALES_ROUTE, CHITCHAT_ROUTE, HUMAN_ROUTE):
            with self.subTest(legacy_hint=legacy_hint):
                context = self._context()
                # 即使旧调用方残留同名动态属性，编排也必须无条件从 Triage 开始。
                context.route_hint = legacy_hint

                async def fake_run(starting_agent, *, input, context, **_kwargs):
                    del input
                    self.assertEqual("Triage Agent", starting_agent.name)
                    self.assertEqual(
                        {
                            "transfer_to_after_sales_agent",
                            "transfer_to_chitchat_agent",
                            "transfer_to_human_agent",
                        },
                        {item.tool_name for item in starting_agent.handoffs},
                    )
                    target = next(
                        item for item in starting_agent.handoffs
                        if item.tool_name == "transfer_to_chitchat_agent"
                    )
                    agent = await target.on_invoke_handoff(
                        SimpleNamespace(context=context),
                        '{"reason":"测试路由","summary":"测试摘要"}',
                    )
                    return SimpleNamespace(last_agent=agent, final_output="你好呀。")

                with patch(
                    "app.dialog.agent_orchestrator.Runner.run",
                    new=AsyncMock(side_effect=fake_run),
                ):
                    result = run_customer_service_agents(
                        context,
                        [{"role": "user", "content": "你好"}],
                        model="test-model",
                    )

                self.assertEqual(CHITCHAT_ROUTE, result.route)

    def test_triage_final_answer_without_handoff_fails_closed_to_human(self) -> None:
        context = self._context()

        async def fake_run(starting_agent, *, input, **_kwargs):
            del input
            return SimpleNamespace(last_agent=starting_agent, final_output="我直接回答了")

        with patch(
            "app.dialog.agent_orchestrator.Runner.run", new=AsyncMock(side_effect=fake_run)
        ):
            result = run_customer_service_agents(
                context,
                [{"role": "user", "content": context.query}],
                model="test-model",
            )

        self.assertEqual(HUMAN_ROUTE, result.route)
        self.assertEqual(context.handoff_reply, result.reply_text)


if __name__ == "__main__":
    unittest.main()
