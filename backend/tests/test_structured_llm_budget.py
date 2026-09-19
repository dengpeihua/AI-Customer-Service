import asyncio
from types import SimpleNamespace

from core.intent_recognizer import IntentCategory, IntentRecognizer
from core.llm_utils import STRUCTURED_OUTPUT_MAX_TOKENS
from evaluation.evaluator import LLMJudge


class RecordingMessages:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.payload)],
        )


class RecordingClient:
    def __init__(self, payload):
        self.messages = RecordingMessages(payload)


def test_intent_structured_call_reserves_room_after_thinking_tokens():
    recognizer = IntentRecognizer(api_key="test", model="test-model")
    client = RecordingClient('{"intent":"refund","confidence":0.9,"reasoning":"退款问题"}')
    recognizer.client = client

    result = asyncio.run(recognizer._llm_recognize("退款多久能到账", history=None))

    assert result["intent"] is IntentCategory.REFUND
    assert client.messages.calls[0]["max_tokens"] == STRUCTURED_OUTPUT_MAX_TOKENS
    assert STRUCTURED_OUTPUT_MAX_TOKENS >= 1024


def test_judge_structured_call_reserves_room_after_thinking_tokens():
    client = RecordingClient(
        '{"relevance":0.9,"accuracy":0.8,"completeness":0.7,"helpfulness":0.85}'
    )
    judge = LLMJudge(client, "test-model")

    result = asyncio.run(judge.judge("退款多久", "通常需要若干工作日"))

    assert result.relevance == 0.9
    assert client.messages.calls[0]["max_tokens"] == STRUCTURED_OUTPUT_MAX_TOKENS
    assert STRUCTURED_OUTPUT_MAX_TOKENS >= 1024


def test_judge_scores_completeness_within_the_current_turn_capability_boundary():
    assert "本轮实际可完成的范围" in LLMJudge.JUDGE_PROMPT
    assert "不要因为响应没有虚构已执行不存在的后台操作而扣分" in LLMJudge.JUDGE_PROMPT
