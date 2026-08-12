from sqlalchemy.orm import Session

from app.config import settings
from app.kb.chunker import chunk_text
from app.kb.vector_store import VectorStore
from app.llm.base import LLM
from app.models.knowledge import KbChunk, KbDocument


def ingest_document(
    db: Session, llm: LLM, tenant_id: int, title: str, source_type: str, content: str
) -> KbDocument:
    doc = KbDocument(tenant_id=tenant_id, title=title, source_type=source_type, status="pending")
    db.add(doc)
    db.flush()  # get doc.id

    pieces = chunk_text(content, settings.rag_chunk_size, settings.rag_chunk_overlap)
    vs = VectorStore(db.connection())
    vs.ensure_schema()
    if pieces:
        vectors = llm.embed(pieces)
        for i, (piece, vec) in enumerate(zip(pieces, vectors)):
            chunk = KbChunk(tenant_id=tenant_id, document_id=doc.id, ord=i, text=piece)
            db.add(chunk)
            db.flush()  # get chunk.id
            vs.add(tenant_id, chunk.id, vec)

    doc.status = "indexed"
    db.commit()
    db.refresh(doc)
    return doc
