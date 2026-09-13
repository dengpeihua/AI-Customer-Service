"""MiniMax M3 对话与 embo-01 向量化。"""
from typing import Any
from urllib.parse import urlparse

from openai import DefaultHttpxClient, OpenAI

from app.config import secret_is_configured, settings
from app.llm.base import EmbeddingInputType, Vector
from app.llm.minimax import MiniMaxEmbeddingClient


def minimax_chat_config() -> tuple[str, str, dict[str, Any]]:
    if not secret_is_configured(settings.minimax_api_key):
        raise ValueError("MINIMAX_API_KEY 未配置")
    base_url = settings.minimax_api_base.rstrip("/")
    endpoint = urlparse(base_url)
    if (
        endpoint.scheme != "https"
        or endpoint.hostname not in {"api.minimaxi.com", "api.minimax.cn"}
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.port not in (None, 443)
        or endpoint.path != "/v1"
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError("MINIMAX_API_BASE 必须使用 MiniMax 官方 HTTPS /v1 端点")
    # M3 支持关闭思考；延续客服的低延迟模式，分离字段避免思考文本混入回复。
    return base_url, settings.minimax_model, {
        "thinking": {"type": "disabled"},
        "reasoning_split": True,
    }


class MiniMaxLLM:
    def __init__(self) -> None:
        base_url, self._chat_model, self._extra_body = minimax_chat_config()
        self._embed_client = MiniMaxEmbeddingClient()
        try:
            self._chat_client = OpenAI(
                api_key=settings.minimax_api_key,
                base_url=base_url,
                timeout=60,
                max_retries=1,
                http_client=DefaultHttpxClient(trust_env=False),
            )
        except Exception:
            self._embed_client.close()
            raise

    def chat(self, system: str, user: str, temperature: float | None = None) -> str:
        kwargs: dict[str, Any] = {}
        if temperature is not None:
            kwargs["temperature"] = temperature
        response = self._chat_client.chat.completions.create(
            model=self._chat_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            extra_body=self._extra_body,
            **kwargs,
        )
        if not response.choices or not response.choices[0].message.content:
            raise RuntimeError("MiniMax 返回空回复")
        return response.choices[0].message.content

    def embed(
        self, texts: list[str], *, input_type: EmbeddingInputType = "db"
    ) -> list[Vector]:
        return self._embed_client.embed(texts, input_type=input_type)

    def close(self) -> None:
        self._chat_client.close()
        self._embed_client.close()
