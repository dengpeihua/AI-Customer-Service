import hashlib
import math
import re

from app.llm.base import EMBED_DIM, EmbeddingInputType, Vector

_WORD = re.compile(r"[\w一-鿿]+")


def _embed_one(text: str) -> Vector:
    # Deterministic bag-of-words hashing: shared words -> aligned vectors.
    vec = [0.0] * EMBED_DIM
    for w in _WORD.findall(text.lower()):
        idx = int.from_bytes(hashlib.md5(w.encode("utf-8")).digest()[:4], "big") % EMBED_DIM
        vec[idx] += 1.0
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


class FakeLLM:
    """Deterministic, offline LLM for tests and no-key development."""

    def embed(
        self, texts: list[str], *, input_type: EmbeddingInputType = "db"
    ) -> list[Vector]:
        return [_embed_one(t) for t in texts]

    def chat(self, system: str, user: str, temperature: float | None = None) -> str:
        return f"[fake-answer] {user.strip()}"
