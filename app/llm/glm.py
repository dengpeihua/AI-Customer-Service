"""Optional GLM-4.7 chat and embedding-3 client for the customer-service backend."""
import math
from typing import Any
from urllib.parse import urlparse

from openai import DefaultHttpxClient, OpenAI

from app.config import GLM_EMBEDDING_DIMENSIONS, secret_is_configured, settings
from app.llm.base import EMBED_DIM, EmbeddingInputType, Vector

EMBED_BATCH = 64


def glm_chat_config() -> tuple[str, str, dict[str, Any]]:
    if not secret_is_configured(settings.glm_api_key):
        raise ValueError("GLM_API_KEY 未配置，请先启用本地 .env 中的备用配置")
    base_url = settings.glm_api_base.rstrip("/")
    endpoint = urlparse(base_url)
    if (
        endpoint.scheme != "https"
        or endpoint.hostname != "open.bigmodel.cn"
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.port not in (None, 443)
        or endpoint.path != "/api/paas/v4"
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError("GLM_API_BASE 必须使用 https://open.bigmodel.cn/api/paas/v4")
    return base_url, settings.glm_model, {"thinking": {"type": "disabled"}}


def glm_temperature(value: float | None) -> float | None:
    """Map the workbench's temperature to the GLM OpenAI-compatible range."""
    return None if value is None else min(1.0, max(0.01, value))


class GLMLLM:
    def __init__(self) -> None:
        base_url, self._chat_model, self._extra_body = glm_chat_config()
        if EMBED_DIM not in GLM_EMBEDDING_DIMENSIONS:
            raise ValueError("GLM embedding-3 需要 LLM_EMBEDDING_DIMENSION=256/512/1024/2048")
        self._dimension = EMBED_DIM
        self._embed_model = settings.glm_embed_model
        self._client = OpenAI(
            api_key=settings.glm_api_key,
            base_url=base_url,
            timeout=60,
            max_retries=1,
            http_client=DefaultHttpxClient(trust_env=False),
        )

    def chat(self, system: str, user: str, temperature: float | None = None) -> str:
        kwargs: dict[str, Any] = {}
        if temperature is not None:
            kwargs["temperature"] = glm_temperature(temperature)
        response = self._client.chat.completions.create(
            model=self._chat_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            extra_body=self._extra_body,
            **kwargs,
        )
        if not response.choices or not response.choices[0].message.content:
            raise RuntimeError("GLM 返回空回复")
        return response.choices[0].message.content

    def embed(
        self, texts: list[str], *, input_type: EmbeddingInputType = "db"
    ) -> list[Vector]:
        if input_type not in ("db", "query"):
            raise ValueError(f"unsupported GLM embedding input type: {input_type}")
        vectors: list[Vector] = []
        for start in range(0, len(texts), EMBED_BATCH):
            batch = texts[start:start + EMBED_BATCH]
            response = self._client.embeddings.create(
                model=self._embed_model,
                input=batch,
                dimensions=self._dimension,
                encoding_format="float",
            )
            # Use the API indices rather than assuming its response order matches the input.
            items = sorted(response.data, key=lambda item: item.index)
            if [item.index for item in items] != list(range(len(batch))):
                raise ValueError("GLM embedding count or indices mismatch")
            for item in items:
                if len(item.embedding) != self._dimension:
                    raise ValueError(
                        f"GLM embedding dimension mismatch: expected={self._dimension} "
                        f"actual={len(item.embedding)}"
                    )
                vector = [float(value) for value in item.embedding]
                if not all(math.isfinite(value) for value in vector):
                    raise ValueError("GLM embedding contains non-finite values")
                vectors.append(vector)
        return vectors

    def close(self) -> None:
        self._client.close()
