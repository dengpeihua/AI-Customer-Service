from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - register all tables
from app.db import Base
from app.dialog.agent_orchestrator import (
    AFTER_SALES_ROUTE,
    CHITCHAT_ROUTE,
    HUMAN_ROUTE,
    AgentRunResult,
)
from app.dialog.engine import _reply_is_supported_by_kb, answer
from app.models.conversation import Conversation, Message


class MemoryAwareDialogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.llm = MagicMock()
        self.memory_service = MagicMock()
        self.memory_service.recall.return_value = {"results": []}

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    @staticmethod
    def _agent_result(
        route: str,
        reply: str,
        *,
        hits: list[dict] | None = None,
        reason: str = "测试路由",
    ) -> AgentRunResult:
        return AgentRunResult(
            route=route,
            reply_text=reply,
            kb_hits=hits or [],
            reason=reason,
            summary="客户需求摘要",
        )

    def _answer_with(self, agent_result: AgentRunResult) -> dict:
        with (
            patch("app.dialog.engine.customer_ai_muted", return_value=False),
            patch(
                "app.dialog.engine.get_customer_memory_service",
                return_value=self.memory_service,
            ),
            patch(
                "app.dialog.engine.run_customer_service_agents",
                return_value=agent_result,
            ),
        ):
            return answer(
                self.db,
                self.llm,
                7,
                "wechat_personal",
                "wxid_friend",
                "客户消息",
                None,
            )

    def test_after_sales_agent_returns_only_grounded_rag_answer(self) -> None:
        result = self._answer_with(self._agent_result(
            AFTER_SALES_ROUTE,
            "订单付款后 48 小时内发货。",
            hits=[{"text": "订单付款后 48 小时内发货。", "distance": 0.1}],
            reason="需要查询发货规则",
        ))

        self.assertEqual(AFTER_SALES_ROUTE, result["intent"])
        self.assertEqual("auto_reply", result["action"])
        self.assertNotIn("confidence", result)
        self.assertEqual("需要查询发货规则", result["route_reason"])
        self.assertIsInstance(result["outbound_message_id"], int)
        self.assertNotIn("semantic_intent", result)
        self.assertNotIn("intent_score", result)
        outbound = self.db.get(Message, result["outbound_message_id"])
        self.assertEqual("pending", outbound.meta["delivery_status"])

    def test_same_channel_message_id_reuses_first_result_without_second_agent_run(self) -> None:
        agent_result = self._agent_result(CHITCHAT_ROUTE, "你好呀。")
        with (
            patch("app.dialog.engine.customer_ai_muted", return_value=False),
            patch(
                "app.dialog.engine.get_customer_memory_service",
                return_value=self.memory_service,
            ),
            patch(
                "app.dialog.engine.run_customer_service_agents",
                return_value=agent_result,
            ) as agents_mock,
        ):
            first = answer(
                self.db, self.llm, 7, "wechat_personal", "wxid_friend",
                "你好", None, source_message_id="wechat:123",
            )
            second = answer(
                self.db, self.llm, 7, "wechat_personal", "wxid_friend",
                "你好", first["conversation_id"], source_message_id="wechat:123",
            )

        self.assertEqual(first["outbound_message_id"], second["outbound_message_id"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual("auto_reply", second["action"])
        self.assertEqual("你好呀", second["reply_text"].rstrip("。"))
        self.assertEqual(1, agents_mock.call_count)
        self.assertEqual(2, self.db.query(Message).count())

    def test_ai_mute_lookup_failure_fails_closed_to_handoff(self) -> None:
        with (
            patch(
                "app.dialog.engine.customer_ai_muted",
                side_effect=RuntimeError("database unavailable"),
            ),
            patch("app.dialog.engine.run_customer_service_agents") as agents_mock,
        ):
            result = answer(
                self.db,
                self.llm,
                7,
                "wechat_personal",
                "wxid_friend",
                "你好",
                None,
            )

        self.assertEqual("handoff", result["action"])
        self.assertEqual(HUMAN_ROUTE, result["intent"])
        agents_mock.assert_not_called()

    def test_every_unmuted_message_goes_directly_to_agent_orchestration(self) -> None:
        captured_context = []

        def fake_agents(context, _input_items):
            captured_context.append(context)
            return self._agent_result(CHITCHAT_ROUTE, "你好呀。")

        with (
            patch("app.dialog.engine.customer_ai_muted", return_value=False),
            patch(
                "app.dialog.engine.get_customer_memory_service",
                return_value=self.memory_service,
            ),
            patch(
                "app.dialog.engine.run_customer_service_agents",
                side_effect=fake_agents,
            ),
        ):
            result = answer(
                self.db,
                self.llm,
                7,
                "wechat_personal",
                "wxid_friend",
                "你好呀",
                None,
            )

        self.assertFalse(hasattr(captured_context[0], "route_hint"))
        self.assertFalse(hasattr(captured_context[0], "query_embedding"))
        self.assertNotIn("semantic_intent", result)
        self.assertNotIn("intent_score", result)

    def test_after_sales_agent_ungrounded_claim_is_escalated_to_human(self) -> None:
        result = self._answer_with(self._agent_result(
            AFTER_SALES_ROUTE,
            "蓝色款有货，支持退款，售价 99 元。",
            hits=[{"text": "蓝色款目前有货。", "distance": 0.1}],
        ))

        self.assertEqual(HUMAN_ROUTE, result["intent"])
        self.assertEqual("handoff", result["action"])
        outbound = self.db.query(Message).filter(Message.direction == "out").one()
        self.assertEqual(AFTER_SALES_ROUTE, outbound.meta["selected_agent"])

    def test_chitchat_agent_uses_contact_scoped_memory(self) -> None:
        self.memory_service.recall.return_value = {"results": [{
            "memory": "客户有一只叫麻将的小狗",
            "score": 0.93,
            "memory_type": "fact",
        }]}
        captured_context = []

        def fake_agents(context, _input_items):
            captured_context.append(context)
            return self._agent_result(CHITCHAT_ROUTE, "麻将听起来是一只很乖的小狗。")

        with (
            patch("app.dialog.engine.customer_ai_muted", return_value=False),
            patch(
                "app.dialog.engine.get_customer_memory_service",
                return_value=self.memory_service,
            ),
            patch(
                "app.dialog.engine.run_customer_service_agents",
                side_effect=fake_agents,
            ),
        ):
            result = answer(
                self.db,
                self.llm,
                7,
                "wechat_personal",
                "wxid_friend",
                "我今天有点难过",
                None,
            )

        self.assertEqual("auto_reply", result["action"])
        self.assertEqual(CHITCHAT_ROUTE, result["intent"])
        self.assertIn("麻将", captured_context[0].memory_note)
        self.memory_service.recall.assert_called_once_with(
            tenant_id=7,
            channel="wechat_personal",
            contact_id="wxid_friend",
            query="我今天有点难过",
            limit=5,
            timeout=1.5,
        )

    def test_chitchat_agent_cannot_add_business_claims(self) -> None:
        result = self._answer_with(self._agent_result(
            CHITCHAT_ROUTE,
            "当然可以，蓝色款售价 99 元并且支持退款。",
        ))

        self.assertEqual(HUMAN_ROUTE, result["intent"])
        self.assertEqual("handoff", result["action"])

    def test_human_agent_enters_existing_pending_handoff_flow(self) -> None:
        result = self._answer_with(self._agent_result(
            HUMAN_ROUTE,
            "模型生成的内容不会发送",
            reason="客户明确要求真人",
        ))

        self.assertEqual(HUMAN_ROUTE, result["intent"])
        self.assertEqual("handoff", result["action"])
        conversation = self.db.get(Conversation, result["conversation_id"])
        self.assertEqual("handoff", conversation.status)

    def test_agent_failure_fails_closed_to_human(self) -> None:
        with (
            patch("app.dialog.engine.customer_ai_muted", return_value=False),
            patch(
                "app.dialog.engine.get_customer_memory_service",
                return_value=self.memory_service,
            ),
            patch(
                "app.dialog.engine.run_customer_service_agents",
                side_effect=RuntimeError("provider unavailable"),
            ),
        ):
            result = answer(
                self.db,
                self.llm,
                7,
                "wechat_personal",
                "wxid_friend",
                "无法识别的消息",
                None,
            )

        self.assertEqual(HUMAN_ROUTE, result["intent"])
        self.assertEqual("handoff", result["action"])
        self.assertIn("RuntimeError", result["route_reason"])

    def test_ai_muted_contact_bypasses_agents_and_memory(self) -> None:
        with (
            patch("app.dialog.engine.customer_ai_muted", return_value=True),
            patch("app.dialog.engine.get_customer_memory_service") as memory_mock,
            patch("app.dialog.engine.run_customer_service_agents") as agents_mock,
        ):
            result = answer(
                self.db,
                self.llm,
                7,
                "wechat_personal",
                "wxid_friend",
                "你好",
                None,
            )

        self.assertEqual("handoff", result["action"])
        agents_mock.assert_not_called()
        memory_mock.assert_not_called()

    def test_agent_input_contains_recent_history_but_not_handoff_notice(self) -> None:
        conversation = Conversation(
            tenant_id=7,
            channel="wechat_personal",
            contact_id="wxid_friend",
            status="active",
        )
        self.db.add(conversation)
        self.db.flush()
        self.db.add_all([
            Message(
                tenant_id=7,
                conversation_id=conversation.id,
                direction="in",
                sender="customer",
                provenance="customer",
                text="我上一条问了什么？",
            ),
            Message(
                tenant_id=7,
                conversation_id=conversation.id,
                direction="out",
                sender="agent",
                provenance="handoff",
                text="帮您转接人工客服回复中，请稍等",
            ),
            Message(
                tenant_id=7,
                conversation_id=conversation.id,
                direction="out",
                sender="agent",
                provenance="human",
                text="您上一条询问了物流。",
            ),
        ])
        self.db.commit()
        captured_inputs = []

        def fake_agents(_context, input_items):
            captured_inputs.append(input_items)
            return self._agent_result(CHITCHAT_ROUTE, "记得，您刚才问了物流。")

        with (
            patch("app.dialog.engine.customer_ai_muted", return_value=False),
            patch(
                "app.dialog.engine.get_customer_memory_service",
                return_value=self.memory_service,
            ),
            patch(
                "app.dialog.engine.run_customer_service_agents",
                side_effect=fake_agents,
            ),
        ):
            answer(
                self.db,
                self.llm,
                7,
                "wechat_personal",
                "wxid_friend",
                "那现在呢？",
                conversation.id,
            )

        self.assertEqual(
            [
                {"role": "user", "content": "我上一条问了什么？"},
                {"role": "assistant", "content": "您上一条询问了物流。"},
                {"role": "user", "content": "那现在呢？"},
            ],
            captured_inputs[0],
        )

    def test_handoff_pair_stays_saved_but_does_not_pollute_next_route(self) -> None:
        captured_inputs = []

        def fake_agents(_context, input_items):
            captured_inputs.append(input_items)
            if len(captured_inputs) == 1:
                return self._agent_result(
                    HUMAN_ROUTE,
                    "模型生成内容不会发送",
                    reason="价格信息需要人工确认",
                )
            return self._agent_result(CHITCHAT_ROUTE, "麻将听起来真的很乖。")

        with (
            patch("app.dialog.engine.customer_ai_muted", return_value=False),
            patch(
                "app.dialog.engine.get_customer_memory_service",
                return_value=self.memory_service,
            ),
            patch(
                "app.dialog.engine.run_customer_service_agents",
                side_effect=fake_agents,
            ),
        ):
            first = answer(
                self.db,
                self.llm,
                7,
                "wechat_personal",
                "wxid_friend",
                "产品现在的价钱是怎样的",
                None,
            )
            second = answer(
                self.db,
                self.llm,
                7,
                "wechat_personal",
                "wxid_friend",
                "我有一只小狗叫麻将，他是一只很乖的小狗",
                first["conversation_id"],
            )

        self.assertEqual("handoff", first["action"])
        self.assertEqual(CHITCHAT_ROUTE, second["intent"])
        self.assertEqual("auto_reply", second["action"])
        serialized = "\n".join(item["content"] for item in captured_inputs[1])
        self.assertNotIn("产品现在的价钱", serialized)
        self.assertNotIn("转接人工", serialized)
        self.assertIn("小狗叫麻将", serialized)
        saved = self.db.query(Message).filter(
            Message.conversation_id == first["conversation_id"]
        ).order_by(Message.id).all()
        self.assertEqual(
            [
                "产品现在的价钱是怎样的",
                "帮您转接人工客服回复中，请稍等",
                "我有一只小狗叫麻将，他是一只很乖的小狗",
                "麻将听起来真的很乖",
            ],
            [message.text for message in saved],
        )

    def test_unanswered_customer_batch_is_used_for_routing_and_memory(self) -> None:
        conversation = Conversation(
            tenant_id=7,
            channel="wechat_personal",
            contact_id="wxid_friend",
            status="active",
        )
        self.db.add(conversation)
        self.db.flush()
        self.db.add_all([
            Message(
                tenant_id=7,
                conversation_id=conversation.id,
                direction="in",
                sender="customer",
                provenance="customer",
                text="我和我弟感情挺好的",
            ),
            Message(
                tenant_id=7,
                conversation_id=conversation.id,
                direction="in",
                sender="customer",
                provenance="customer",
                text="我们经常一起打游戏",
            ),
        ])
        self.db.commit()
        captured_contexts = []

        def fake_agents(context, _input_items):
            captured_contexts.append(context)
            return self._agent_result(CHITCHAT_ROUTE, "听起来你们相处得很好。")

        with (
            patch("app.dialog.engine.customer_ai_muted", return_value=False),
            patch(
                "app.dialog.engine.get_customer_memory_service",
                return_value=self.memory_service,
            ),
            patch(
                "app.dialog.engine.run_customer_service_agents",
                side_effect=fake_agents,
            ),
        ):
            result = answer(
                self.db,
                self.llm,
                7,
                "wechat_personal",
                "wxid_friend",
                "我有一只小狗叫麻将",
                conversation.id,
            )

        self.assertEqual(CHITCHAT_ROUTE, result["intent"])
        routing_query = captured_contexts[0].query
        self.assertIn("我和我弟感情挺好的", routing_query)
        self.assertIn("我们经常一起打游戏", routing_query)
        self.assertIn("我有一只小狗叫麻将", routing_query)
        self.memory_service.recall.assert_called_once()
        recall_query = self.memory_service.recall.call_args.kwargs["query"]
        self.assertEqual(routing_query, recall_query)

    def test_foreign_contact_conversation_id_is_never_reused(self) -> None:
        foreign = Conversation(
            tenant_id=7,
            channel="wechat_personal",
            contact_id="contact-a",
            status="active",
        )
        self.db.add(foreign)
        self.db.commit()

        with (
            patch("app.dialog.engine.customer_ai_muted", return_value=False),
            patch(
                "app.dialog.engine.get_customer_memory_service",
                return_value=self.memory_service,
            ),
            patch(
                "app.dialog.engine.run_customer_service_agents",
                return_value=self._agent_result(CHITCHAT_ROUTE, "您好。"),
            ),
        ):
            result = answer(
                self.db,
                self.llm,
                7,
                "wechat_personal",
                "contact-b",
                "你好",
                foreign.id,
            )

        self.assertNotEqual(foreign.id, result["conversation_id"])
        self.assertEqual(
            [],
            self.db.query(Message).filter(Message.conversation_id == foreign.id).all(),
        )

    def test_grounding_gate_rejects_chinese_and_english_claim_synonyms(self) -> None:
        kb = "蓝色款目前有货"
        self.assertFalse(_reply_is_supported_by_kb("蓝色款只卖九十九元，可以原路退钱", kb))
        self.assertFalse(_reply_is_supported_by_kb("costs ninety nine dollars and is refundable", kb))
        self.assertFalse(_reply_is_supported_by_kb("现货充足，我们确保明日处理", kb))

    def test_grounding_gate_rejects_policy_claim_absent_from_kb(self) -> None:
        kb = "订单付款后 48 小时内发货。"
        self.assertFalse(
            _reply_is_supported_by_kb("根据我们的售后政策，付款后 48 小时内发货。", kb)
        )


if __name__ == "__main__":
    unittest.main()
