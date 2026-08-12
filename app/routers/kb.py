from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.deps import CurrentUser
from app.kb.ingest import ingest_document
from app.kb.summarize import (
    KbSummarizeError,
    summarize_conversation_to_kb,
    summarize_texts_to_kb,
)
from app.kb.upload import KbUploadError, ingest_upload
from app.llm import get_llm
from app.models.knowledge import KbDocument

router = APIRouter(prefix="/v1/kb", tags=["kb"])


class DocIn(BaseModel):
    title: str
    source_type: str = "faq"
    content: str = ""


class DocOut(BaseModel):
    id: int
    title: str
    source_type: str
    status: str


@router.post("/documents", response_model=DocOut)
def create_document(
    body: DocIn, user: CurrentUser, db: Annotated[Session, Depends(get_db)]
):
    doc = ingest_document(
        db, get_llm(), tenant_id=user.tenant_id,
        title=body.title, source_type=body.source_type, content=body.content,
    )
    return DocOut(id=doc.id, title=doc.title, source_type=doc.source_type, status=doc.status)


@router.post("/documents/upload", response_model=DocOut)
async def upload_document(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    file: UploadFile = File(...),
    title: str | None = Form(None),
):
    # 最多读到上限+1 字节，避免超大文件把内存吃穿
    raw = await file.read(settings.kb_upload_max_bytes + 1)
    try:
        doc = ingest_upload(
            db, get_llm(), tenant_id=user.tenant_id,
            filename=file.filename or "", raw=raw, title=title,
        )
    except KbUploadError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return DocOut(id=doc.id, title=doc.title, source_type=doc.source_type, status=doc.status)


@router.get("/documents", response_model=list[DocOut])
def list_documents(user: CurrentUser, db: Annotated[Session, Depends(get_db)]):
    docs = db.scalars(
        select(KbDocument)
        .where(KbDocument.tenant_id == user.tenant_id)
        .order_by(KbDocument.id)
    ).all()
    return [
        DocOut(id=d.id, title=d.title, source_type=d.source_type, status=d.status)
        for d in docs
    ]


class SummarizeIn(BaseModel):
    channel: str
    contact_id: str


class SummarizeOut(BaseModel):
    doc_id: int
    title: str


@router.post("/summarize", response_model=SummarizeOut)
def summarize_to_kb(body: SummarizeIn, user: CurrentUser, db: Annotated[Session, Depends(get_db)]):
    try:
        doc = summarize_conversation_to_kb(db, get_llm(), tenant_id=user.tenant_id,
                                           channel=body.channel, contact_id=body.contact_id)
    except KbSummarizeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return SummarizeOut(doc_id=doc.id, title=doc.title)


class SummarizeTextsIn(BaseModel):
    texts: list[str]
    title: str = "企微本地历史反哺"


@router.post("/summarize-texts", response_model=SummarizeOut)
def summarize_texts(body: SummarizeTextsIn, user: CurrentUser,
                    db: Annotated[Session, Depends(get_db)]):
    """把企微本地库收割的历史消息文本提炼成 FAQ 反哺知识库（LLM 蒸馏，非裸转储）。
    供挂件「拉取企微历史 → 反哺」按钮调用。租户隔离由 CurrentUser 保证。"""
    try:
        doc = summarize_texts_to_kb(db, get_llm(), tenant_id=user.tenant_id,
                                    texts=body.texts, title=body.title)
    except KbSummarizeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return SummarizeOut(doc_id=doc.id, title=doc.title)
