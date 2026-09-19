import asyncio

from agents.agent_orchestrator import (
    AgentProfile,
    AgentResponse,
    AgentType,
    AgentOrchestrator,
    BillingAgent,
    EscalationAgent,
    GeneralAgent,
    Request,
    ResponseComposer,
    RoutingDecision,
    TechnicalAgent,
    build_shared_rag_tools,
)
from core.intent_recognizer import IntentCategory, UrgencyLevel


class FakeClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

        class Messages:
            async def create(inner, **kwargs):
                self.calls.append(kwargs)
                if self.error:
                    raise self.error
                return self.response

        self.messages = Messages()


def make_request(**kwargs):
    values = {
        "message": "登录时报 401，同时这笔订单被重复扣款",
        "user_id": "u1",
        "conv_id": "c1",
        "intent": IntentCategory.TECHNICAL_LOGIN,
        "intent_group": "technical",
        "urgency": UrgencyLevel.HIGH,
        "intent_confidence": 0.92,
        "entities": {"error_code": ["401"], "amount": ["99 元"]},
    }
    values.update(kwargs)
    return Request(**values)


def test_agent_profiles_have_distinct_contracts_and_generation_config():
    assert isinstance(GeneralAgent.profile, AgentProfile)
    assert GeneralAgent.profile.role != TechnicalAgent.profile.role
    assert TechnicalAgent.profile.workflow != BillingAgent.profile.workflow
    assert TechnicalAgent.profile.temperature < GeneralAgent.profile.temperature
    assert "search_knowledge_base" in GeneralAgent.profile.tool_scope
    assert "lookup_error_code" in TechnicalAgent.profile.tool_scope
    assert "check_billing_fields" in BillingAgent.profile.tool_scope


def test_domain_agents_build_different_role_packets():
    req = make_request()
    general_packet = GeneralAgent(FakeClient(), "test-model")._build_role_packet(req)
    technical_packet = TechnicalAgent(FakeClient(), "test-model")._build_role_packet(req)
    billing_packet = BillingAgent(FakeClient(), "test-model")._build_role_packet(req)

    assert "triage_targets" in general_packet
    assert "diagnostic_fields" in technical_packet
    assert "verification_fields" in billing_packet
    assert general_packet != technical_packet != billing_packet


def test_escalation_agent_is_a_real_non_llm_handoff_node():
    client = FakeClient()
    agent = EscalationAgent(client, "test-model")

    result = asyncio.run(agent.handle(make_request(
        intent=IntentCategory.HUMAN_HANDOFF,
        urgency=UrgencyLevel.CRITICAL,
    )))

    assert result.success is True
    assert result.escalate is True
    assert "人工升级" in result.content
    assert client.calls == []


def test_composer_fallback_preserves_primary_and_supporting_results():
    composer = ResponseComposer(FakeClient(error=RuntimeError("provider down")), "test-model")
    req = make_request()
    responses = [
        AgentResponse(AgentType.TECHNICAL, "先排查 Token 是否过期。", True),
        AgentResponse(AgentType.BILLING, "请提供两笔扣款的时间和金额。", True),
    ]

    content = asyncio.run(composer.compose(req, responses))

    assert content.startswith("先排查 Token 是否过期。")
    assert "补充说明" in content
    assert "两笔扣款" in content


def test_routing_decision_can_target_escalation_pool():
    # Keep this assertion close to the public data contract used by the API.
    decision = RoutingDecision(
        primary_agent=AgentType.ESCALATION,
        reason="critical request",
        confidence=1.0,
    )
    assert decision.agent_types == [AgentType.ESCALATION]
    assert not decision.multi_agent


def test_composite_request_routes_explicit_billing_signal_as_supporting_agent():
    orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
    orchestrator._pool = {
        AgentType.GENERAL: [object()],
        AgentType.TECHNICAL: [object()],
        AgentType.BILLING: [object()],
    }

    decision = orchestrator._route_decision(make_request())

    assert decision.primary_agent is AgentType.TECHNICAL
    assert decision.supporting_agents == [AgentType.BILLING]
    assert decision.multi_agent is True


def test_follow_up_order_number_inherits_recent_refund_intent():
    req = make_request(
        message="订单号是 #12345",
        intent=IntentCategory.OTHER,
        intent_group="other",
        urgency=UrgencyLevel.LOW,
        intent_confidence=0.2,
        history=[
            {"role": "user", "content": "你好，我想退款"},
            {"role": "assistant", "content": "请提供订单号或交易号。"},
        ],
    )

    AgentOrchestrator._inherit_contextual_intent(req)

    assert req.intent is IntentCategory.REFUND
    assert req.intent_group == "billing"
    assert req.intent_confidence == 0.8
    assert AgentOrchestrator._needs_clarification(req) is False


def test_agent_tool_scopes_are_real_and_isolated():
    general_tools = set(GeneralAgent(FakeClient(), "test-model").get_tools())
    technical_tools = set(TechnicalAgent(FakeClient(), "test-model").get_tools())
    billing_tools = set(BillingAgent(FakeClient(), "test-model").get_tools())
    escalation_tools = set(EscalationAgent(FakeClient(), "test-model").get_tools())

    assert general_tools == {"inspect_request_context", "suggest_required_fields"}
    assert technical_tools == {"lookup_error_code", "build_diagnostic_plan"}
    assert billing_tools == {"check_billing_fields", "compare_amounts"}
    assert escalation_tools == {"create_handoff_summary"}
    assert not general_tools & technical_tools
    assert not technical_tools & billing_tools


def test_shared_rag_tool_is_available_to_all_agents():
    class RagManager:
        async def search_with_rewrite(self, tool_name, query, top_k=5):
            return type(
                "Result",
                (),
                {"success": True, "data": [{"title": "退款政策", "content": "7 天内可退款"}], "reranked": True},
            )()

    shared = build_shared_rag_tools(RagManager())

    general = GeneralAgent(FakeClient(), "test-model")
    technical = TechnicalAgent(FakeClient(), "test-model")
    billing = BillingAgent(FakeClient(), "test-model")
    escalation = EscalationAgent(FakeClient(), "test-model")

    for agent in (general, technical, billing, escalation):
        agent.set_shared_tools(shared)
        tools = agent.get_tools()
        assert "search_knowledge_base" in tools


def test_tool_input_validation_rejects_unknown_fields():
    agent = TechnicalAgent(FakeClient(), "test-model")
    spec = agent.get_tools()["lookup_error_code"]

    try:
        agent._validate_tool_input(spec, {"error_code": "401", "secret": "nope"})
    except ValueError as exc:
        assert "不允许的工具参数" in str(exc)
    else:
        raise AssertionError("unknown tool fields should be rejected")


def test_tool_use_round_trip_executes_only_whitelisted_tool():
    class ToolUseBlock:
        type = "tool_use"
        id = "toolu_1"
        name = "lookup_error_code"
        input = {"error_code": "401"}

    class TextBlock:
        type = "text"
        text = "已根据 401 错误码给出排查建议。"

    class ToolClient:
        def __init__(self):
            self.calls = []
            self.responses = [
                type("Response", (), {"content": [ToolUseBlock()]})(),
                type("Response", (), {"content": [TextBlock()]})(),
            ]

        class Messages:
            def __init__(self, owner):
                self.owner = owner

            async def create(self, **kwargs):
                self.owner.calls.append(kwargs)
                return self.owner.responses.pop(0)

        @property
        def messages(self):
            return self.Messages(self)

    client = ToolClient()
    agent = TechnicalAgent(client, "test-model")
    response = asyncio.run(agent.handle(make_request()))

    assert response.success is True
    assert response.tools_used == ["lookup_error_code"]
    assert len(client.calls) == 2
    assert {tool["name"] for tool in client.calls[0]["tools"]} == {
        "lookup_error_code",
        "build_diagnostic_plan",
    }
    assert "tool_result" in str(client.calls[1]["messages"])
    assert len(response.tool_traces) == 1
    assert response.tool_traces[0]["tool_name"] == "lookup_error_code"
    assert response.tool_traces[0]["input"] == {"error_code": "401"}
    assert response.tool_traces[0]["success"] is True


def test_address_change_role_packet_requires_complete_safe_clarification():
    req = make_request(
        message="帮我把收货地址改成北京市朝阳区",
        intent=IntentCategory.REQUEST,
        intent_group="service",
        entities={},
    )

    packet = GeneralAgent(FakeClient(), "test-model")._build_role_packet(req)

    assert "订单号或下单时间" in packet
    assert "完整的新收货地址" in packet
    assert "是否已经发货" in packet
    assert "不得声称已经修改" in packet

    completed = GeneralAgent(FakeClient(), "test-model")._finalize_response(
        req,
        "可以处理，请提供具体地址。",
    )
    assert "订单号或下单时间" in completed
    assert "完整的新收货地址" in completed
    assert "是否已经发货" in completed
    assert "不能确认地址已经修改" in completed

    billing_completed = BillingAgent(FakeClient(), "test-model")._finalize_response(
        req,
        "请到订单页面尝试修改。",
    )
    assert "订单号或下单时间" in billing_completed
    assert "不能确认地址已经修改" in billing_completed


def test_shipping_address_change_routes_to_general_even_if_intent_is_account():
    orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
    orchestrator._pool = {
        AgentType.GENERAL: [object()],
        AgentType.TECHNICAL: [object()],
        AgentType.BILLING: [object()],
    }
    req = make_request(
        message="帮我把收货地址改成北京市朝阳区",
        intent=IntentCategory.ACCOUNT,
        intent_group="account",
        entities={},
    )

    decision = orchestrator._route_decision(req)

    assert decision.primary_agent is AgentType.GENERAL
    assert decision.supporting_agents == []


def test_technical_401_response_has_actions_verification_and_safe_boundary():
    req = make_request(message="应用登录一直报错 401")

    response = TechnicalAgent(FakeClient(), "test-model")._finalize_response(req, "请补充信息。")

    assert "401 表示认证失败" in response
    assert "重新登录" in response
    assert "Token" in response
    assert "验证是否恢复" in response
    assert "不要提供完整密码" in response


def test_billing_overcharge_response_gives_immediate_checks_and_required_evidence():
    req = make_request(
        message="为什么这个月多扣了 50 块钱？",
        intent=IntentCategory.PAYMENT_ISSUE,
        intent_group="billing",
        entities={"amount": ["50 元"]},
    )

    response = BillingAgent(FakeClient(), "test-model")._finalize_response(req, "需要核验。")

    assert "自动续费" in response
    assert "套餐升级" in response
    assert "重复扣款" in response
    assert "订单号或交易号" in response
    assert "不能直接认定" in response

    another_amount = BillingAgent(FakeClient(), "test-model")._finalize_response(
        make_request(
            message="为什么重复扣款 88 元？",
            intent=IntentCategory.PAYMENT_ISSUE,
            intent_group="billing",
            entities={"amount": ["88 元"]},
        ),
        "需要核验。",
    )
    assert "88 元" in another_amount
    assert "50 元" not in another_amount


def test_refund_arrival_response_uses_knowledge_base_sla_without_invented_channel_times():
    req = make_request(
        message="退款多久能到账？",
        intent=IntentCategory.REFUND,
        intent_group="billing",
        entities={"order_id": ["#12345"]},
    )

    response = BillingAgent(FakeClient(), "test-model")._finalize_response(req, "以渠道为准。")

    assert "审核通常需要 1-3 个工作日" in response
    assert "审核通过后 5-7 个工作日" in response
    assert "原支付账户" in response
    assert "退款流水号" in response


def test_overdue_order_response_uses_known_order_and_gives_traceable_next_steps():
    req = make_request(
        message="我的订单 #12345 还没到，已经超时了",
        intent=IntentCategory.LOGISTICS,
        intent_group="order",
        entities={"order_id": ["#12345"]},
    )

    response = GeneralAgent(FakeClient(), "test-model")._finalize_response(req, "请提供订单号。")

    assert "已记录订单号 #12345" in response
    assert "发货后 24 小时" in response
    assert "超过 7 天" in response
    assert "申请查件" in response
    assert "预计送达时间" in response
    assert "最新位置" in response
    assert "异常签收" in response
    assert "查件编号" in response
    assert "不能承诺送达日期" in response
    assert "请提供完整订单号" not in response


def test_boundary_explanation_does_not_force_escalation_but_explicit_handoff_does():
    agent = GeneralAgent(FakeClient(), "test-model")

    assert not agent._needs_escalation("若订单已经发货，可能需要联系人工客服核实承运状态。")
    assert agent._needs_escalation("当前问题建议转人工处理。")


def test_address_finalizer_does_not_request_a_complete_address_twice():
    req = make_request(
        message="帮我把订单 #12345 的收货地址改到北京市朝阳区建国路88号，联系人张三，13800000000",
        intent=IntentCategory.REQUEST,
        intent_group="general",
        entities={"order_id": ["#12345"]},
    )

    response = GeneralAgent(FakeClient(), "test-model")._finalize_response(req, "请确认是否已经发货。")

    assert "完整的新收货地址" not in response


def test_address_finalizer_disclaims_unsupported_ticket_or_team_handoff():
    req = make_request(
        message="帮我把订单 #12345 的收货地址改成北京市朝阳区建国路88号，订单未发货",
        intent=IntentCategory.REQUEST,
        intent_group="general",
        entities={"order_id": ["#12345"]},
    )

    response = GeneralAgent(FakeClient(), "test-model")._finalize_response(
        req,
        "修改请求已记录，我会转交相关团队处理。",
    )

    assert "没有实际提交、转交或创建工单" in response
