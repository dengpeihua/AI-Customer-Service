from typing import Literal, Protocol

from app.config import settings

EMBED_DIM = settings.llm_embedding_dimension
Vector = list[float]
EmbeddingInputType = Literal["db", "query"]


class LLM(Protocol):
    def chat(self, system: str, user: str, temperature: float | None = None) -> str: ...
    def embed(
        self, texts: list[str], *, input_type: EmbeddingInputType = "db"
    ) -> list[Vector]: ...
