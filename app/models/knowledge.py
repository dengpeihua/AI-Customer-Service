from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import TimestampMixin


class KbDocument(TimestampMixin, Base):
    __tablename__ = "kb_document"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    source_type: Mapped[str] = mapped_column(String(20))  # faq|docx|pdf|txt|json
    status: Mapped[str] = mapped_column(String(20), default="pending")
    # 反哺来源会话 id：同一会话再次总结时据此去重（幂等），非反哺文档为 NULL。
    # 只作去重键、无 ORM 关系，故用普通列而非 ForeignKey（SQLite 不强制外键，batch 加具名FK 也麻烦）。
    source_conversation_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True
    )


class KbChunk(TimestampMixin, Base):
    __tablename__ = "kb_chunk"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"), index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("kb_document.id"), index=True)
    ord: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
