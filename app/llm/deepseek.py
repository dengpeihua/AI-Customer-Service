"""DeepSeek 对话与 MiniMax embedding 的组合 LLM。"""
from urllib.parse import urlparse

from openai import DefaultHttpxClient, OpenAI

from app.config import secret_is_configured, settings
from app.llm.base import EmbeddingInputType, Vector
from app.llm.minimax import MiniMaxEmbeddingClient


class DeepSeekLLM:
    def __init__(self) -> None:
        if not secret_is_configured(settings.deepseek_api_key):
            raise ValueError("DEEPSEEK_API_KEY 未配置")
        if not secret_is_configured(settings.minimax_api_key):
            raise ValueError("MINIMAX_API_KEY 未配置，无法生成 embedding")
        endpoint = urlparse(settings.deepseek_api_base)
        if endpoint.scheme != "https" or endpoint.hostname != "api.deepseek.com":
            raise ValueError(
                "DEEPSEEK_API_BASE 必须使用 DeepSeek 官方 HTTPS 端点 "
                "https://api.deepseek.com"
            )

        self._chat_client = OpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_api_base,
            timeout=60,
            max_retries=1,
            http_client=DefaultHttpxClient(trust_env=False),
        )
        self._embed_client = MiniMaxEmbeddingClient()
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
