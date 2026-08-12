from sqlalchemy.orm import Session

from app.kb.vector_store import VectorStore
from app.llm.base import LLM
from app.models.knowledge import KbChunk


def retrieve(
    db: Session,
    llm: LLM,
    tenant_id: int,
    query: str,
    k: int,
) -> list[dict]:
    qvec = llm.embed([query], input_type="query")[0]
    vs = VectorStore(db.connection())
    vs.ensure_schema()
    hits = vs.search(tenant_id=tenant_id, query=qvec, k=k)
    out: list[dict] = []
    for chunk_id, distance in hits:
        chunk = db.get(KbChunk, chunk_id)
        if chunk is not None and chunk.tenant_id == tenant_id:
            out.append({"chunk_id": chunk_id, "text": chunk.text, "distance": distance})
    return out
