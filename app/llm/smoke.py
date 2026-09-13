from dataclasses import dataclass

from app.config import settings
from app.llm import chat_model_name, embedding_model_name, get_llm
from app.llm.base import EMBED_DIM, LLM


@dataclass(frozen=True)
class SmokeResult:
    embedding_dimension: int
    reply_length: int


def run_smoke_check(llm: LLM) -> SmokeResult:
    vectors = llm.embed(["customer service connection check"])
    if len(vectors) != 1 or len(vectors[0]) != EMBED_DIM:
        actual = len(vectors[0]) if vectors else 0
        raise RuntimeError(
            f"embedding dimension mismatch: expected={EMBED_DIM} actual={actual}"
        )

    reply = llm.chat(
        "You are a connection health check. Reply briefly.",
        "Reply with OK.",
    ).strip()
    if not reply:
        raise RuntimeError("model returned an empty reply")
    if "[fake-answer]" in reply.lower():
        raise RuntimeError("model returned a fake answer")

    return SmokeResult(
        embedding_dimension=len(vectors[0]),
        reply_length=len(reply),
    )


def main() -> None:
    if settings.llm_provider not in {"deepseek", "minimax", "glm"}:
        raise RuntimeError(
            f"real-provider smoke check requires minimax/glm/deepseek, got {settings.llm_provider}"
        )
    result = run_smoke_check(get_llm())
    print(
        "llm_smoke=ok "
        f"provider={settings.llm_provider} "
        f"chat_model={chat_model_name()} "
        f"embed_model={embedding_model_name()} "
        f"embedding_dimension={result.embedding_dimension} "
        f"reply_length={result.reply_length}"
    )


if __name__ == "__main__":
    main()
