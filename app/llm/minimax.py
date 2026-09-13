from urllib.parse import urlparse

import httpx

from app.config import secret_is_configured, settings
from app.llm.base import EMBED_DIM, EmbeddingInputType, Vector


EMBED_BATCH = 100


def _embedding_endpoint() -> str:
    base_url = settings.minimax_embedding_base_url.rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or parsed.hostname != "api.minimaxi.com":
        raise ValueError(
            "MINIMAX_EMBEDDING_BASE_URL 必须使用 MiniMax 官方 HTTPS 端点 "
            "https://api.minimaxi.com/v1"
        )
    return f"{base_url}/embeddings"


def _client() -> httpx.Client:
    if not secret_is_configured(settings.minimax_api_key):
        raise ValueError("MINIMAX_API_KEY 未配置")
    _embedding_endpoint()
    return httpx.Client(
        headers={
            "Authorization": f"Bearer {settings.minimax_api_key}",
            "Content-Type": "application/json",
        },
        timeout=60,
        trust_env=False,
    )


class MiniMaxEmbeddingClient:
    """MiniMax native embedding client."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._endpoint = _embedding_endpoint()
        self._client = client or _client()
        self._owns_client = client is None
        self._embed_model = settings.minimax_embed_model

    def embed(
        self, texts: list[str], *, input_type: EmbeddingInputType = "db"
    ) -> list[Vector]:
        if input_type not in ("db", "query"):
            raise ValueError(f"unsupported MiniMax embedding input type: {input_type}")
        vectors: list[Vector] = []
        for start in range(0, len(texts), EMBED_BATCH):
            batch = texts[start:start + EMBED_BATCH]
            response = self._client.post(
                self._endpoint,
                json={"model": self._embed_model, "texts": batch, "type": input_type},
            )
            response.raise_for_status()
            payload = response.json()
            base_resp = payload.get("base_resp") if isinstance(payload, dict) else None
            status_code = base_resp.get("status_code") if isinstance(base_resp, dict) else None
            status_message = base_resp.get("status_msg") if isinstance(base_resp, dict) else None
            if status_code != 0:
                raise RuntimeError(
                    "MiniMax embedding API failed: "
                    f"status_code={status_code!r} status_msg={status_message!r}"
                )
            batch_vectors = payload.get("vectors")
            if not isinstance(batch_vectors, list) or len(batch_vectors) != len(batch):
                actual = len(batch_vectors) if isinstance(batch_vectors, list) else None
                raise ValueError(
                    "MiniMax embedding count mismatch: "
                    f"expected={len(batch)} actual={actual!r}"
                )
            for index, vector in enumerate(batch_vectors):
                if not isinstance(vector, list) or len(vector) != EMBED_DIM:
                    actual = len(vector) if isinstance(vector, list) else None
                    raise ValueError(
                        "MiniMax embedding dimension mismatch: "
                        f"index={start + index} expected={EMBED_DIM} actual={actual!r}"
                    )
                vectors.append([float(value) for value in vector])
        return vectors

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
