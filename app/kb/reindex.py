from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.kb.index_verification import verify_index
from app.kb.vector_store import VectorStore
from app.llm import embedding_model_name, get_llm
from app.llm.base import EMBED_DIM, LLM
from app.models.knowledge import KbChunk


@dataclass(frozen=True)
class ReindexResult:
    chunks: int
    vectors: int


def reindex_all(db: Session, llm: LLM) -> ReindexResult:
    chunks = list(
        db.scalars(
            select(KbChunk).order_by(KbChunk.tenant_id, KbChunk.id)
        )
    )
    vectors = llm.embed([chunk.text for chunk in chunks]) if chunks else []

    if len(vectors) != len(chunks):
        raise ValueError(
            f"embedding count mismatch: chunks={len(chunks)} vectors={len(vectors)}"
        )
    for index, vector in enumerate(vectors):
        if len(vector) != EMBED_DIM:
            raise ValueError(
                f"embedding dimension mismatch at index {index}: "
                f"expected={EMBED_DIM} actual={len(vector)}"
            )

    store = VectorStore(db.connection())
    try:
        store.recreate_schema()
        for chunk, vector in zip(chunks, vectors, strict=True):
            store.add(chunk.tenant_id, chunk.id, vector)
        db.commit()
    except Exception:
        db.rollback()
        raise

    return ReindexResult(chunks=len(chunks), vectors=len(vectors))


def main() -> None:
    llm = get_llm()
    with SessionLocal() as db:
        result = reindex_all(db, llm)
        verification = verify_index(db.connection())
    print(
        "reindex=ok "
        f"chunks={result.chunks} vectors={result.vectors} "
        f"dimension={verification.dimension} "
        f"provider={settings.llm_provider} "
        f"embed_model={embedding_model_name()}"
    )


if __name__ == "__main__":
    main()
