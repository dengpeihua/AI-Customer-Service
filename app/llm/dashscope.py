from urllib.parse import urlparse

from openai import DefaultHttpxClient, OpenAI

from app.config import settings
from app.llm.base import EmbeddingInputType, Vector

# DashScope 的 embedding 接口单次最多 10 条，超了报 "batch size is invalid"
EMBED_BATCH = 10


def _client() -> OpenAI:
    if not settings.dashscope_api_key:
        raise ValueError("DASHSCOPE_API_KEY 未配置")
    parsed = urlparse(settings.dashscope_base_url)
    if parsed.scheme != "https" or parsed.hostname != "dashscope.aliyuncs.com":
        raise ValueError(
            "DASHSCOPE_BASE_URL 必须使用阿里云百炼 "
            "https://dashscope.aliyuncs.com"
        )
    return OpenAI(
        api_key=settings.dashscope_api_key,
        base_url=settings.dashscope_base_url,
        timeout=60,
        max_retries=1,
        http_client=DefaultHttpxClient(trust_env=False),
    )


class DashScopeEmbeddingClient:
    """DashScope OpenAI-compatible embedding client shared by all chat providers."""

    def __init__(self, client: OpenAI | None = None) -> None:
        self._client = client or _client()
        self._owns_client = client is None
        self._embed_model = settings.llm_embed_model

    def embed(
        self, texts: list[str], *, input_type: EmbeddingInputType = "db"
    ) -> list[Vector]:
        del input_type  # DashScope's compatible endpoint does not distinguish query/document modes.
        vectors: list[Vector] = []
        for start in range(0, len(texts), EMBED_BATCH):
            batch = texts[start:start + EMBED_BATCH]
            response = self._client.embeddings.create(
                model=self._embed_model, input=batch
            )
            items = sorted(response.data, key=lambda item: getattr(item, "index", 0))
            vectors.extend(list(item.embedding) for item in items)
        return vectors

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


class DashScopeLLM:
    """通义千问 via OpenAI-compatible endpoint (DashScope)."""

    def __init__(self) -> None:
        self._client = _client()
        self._embed_client = DashScopeEmbeddingClient(self._client)
        self._chat_model = settings.llm_chat_model

    def chat(self, system: str, user: str, temperature: float | None = None) -> str:
        kwargs: dict = {}
        if temperature is not None:
            kwargs["temperature"] = temperature
        resp = self._client.chat.completions.create(
            model=self._chat_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            **kwargs,
        )
        return resp.choices[0].message.content or ""

    def close(self) -> None:
        self._client.close()

    def embed(
        self, texts: list[str], *, input_type: EmbeddingInputType = "db"
    ) -> list[Vector]:
        return self._embed_client.embed(texts, input_type=input_type)
