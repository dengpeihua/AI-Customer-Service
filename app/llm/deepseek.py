"""DeepSeek 对话 + DashScope embedding 的组合 LLM。

DeepSeek API 当前没有知识库所需的 embedding 模型，因此只把 ``chat`` 路由到
DeepSeek；``embed`` 交给 DashScope ``text-embedding-v3``。
"""
from openai import DefaultHttpxClient, OpenAI

from app.config import settings
from app.llm.base import EmbeddingInputType, Vector
from app.llm.dashscope import DashScopeEmbeddingClient


class DeepSeekLLM:
    def __init__(self) -> None:
        if not settings.deepseek_api_key:
            raise ValueError("DEEPSEEK_API_KEY 未配置")
        if not settings.dashscope_api_key:
            raise ValueError("DASHSCOPE_API_KEY 未配置，无法生成知识库 embedding")

        self._chat_client = OpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_api_base,
            timeout=60,
            max_retries=1,
            http_client=DefaultHttpxClient(trust_env=False),
        )
        self._embed_client = DashScopeEmbeddingClient()
        self._chat_model = settings.deepseek_model

    def chat(self, system: str, user: str, temperature: float | None = None) -> str:
        kwargs: dict = {}
        if temperature is not None:
            kwargs["temperature"] = temperature
        response = self._chat_client.chat.completions.create(
            model=self._chat_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            extra_body={
                "thinking": {
                    "type": "enabled" if settings.deepseek_thinking else "disabled"
                }
            },
            **kwargs,
        )
        return response.choices[0].message.content or ""

    def embed(
        self, texts: list[str], *, input_type: EmbeddingInputType = "db"
    ) -> list[Vector]:
        return self._embed_client.embed(texts, input_type=input_type)

    def close(self) -> None:
        self._chat_client.close()
        self._embed_client.close()
