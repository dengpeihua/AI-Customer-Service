from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.kb.vector_store import VectorStore
from app.models.knowledge import KbChunk, KbDocument


def list_documents(db: Session, tenant_id: int) -> list[KbDocument]:
    return list(db.scalars(select(KbDocument).where(KbDocument.tenant_id == tenant_id)
                           .order_by(KbDocument.id.desc())).all())


def delete_document(db: Session, tenant_id: int, doc_id: int) -> None:
    doc = db.get(KbDocument, doc_id)
    if doc is None or doc.tenant_id != tenant_id:
        return
    chunk_ids = list(db.scalars(select(KbChunk.id).where(
        KbChunk.tenant_id == tenant_id, KbChunk.document_id == doc_id)))
    vs = VectorStore(db.connection())
    vs.ensure_schema()
    vs.delete(tenant_id, chunk_ids)
    db.execute(delete(KbChunk).where(KbChunk.tenant_id == tenant_id, KbChunk.document_id == doc_id))
    db.delete(doc)
    db.commit()
