import datetime as dt

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import TimestampMixin


class CustomerMemory(TimestampMixin, Base):
    """租户内的客户长期记忆。

    ``source_key`` 只用于自动生成条目的幂等更新；人工记忆保持为 ``NULL``，因此同一客户可以
    保存多条同类型内容。记忆不会替代知识库，只作为回复个性化和客服跟进的上下文。
    """

    __tablename__ = "customer_memory"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "channel", "contact_id", "source_key",
            name="uq_customer_memory_source",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"), index=True)
    channel: Mapped[str] = mapped_column(String(30), index=True)
    contact_id: Mapped[str] = mapped_column(String(120), index=True)
    memory_type: Mapped[str] = mapped_column(String(24), default="note", index=True)
    content: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(40), default="manual")
    source_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MemorySyncState(TimestampMixin, Base):
    """真实聊天到 Mem0 的增量摄取游标。

    一位联系人只保留一个进度摘要。逐条幂等由 ``MemoryIngestedMessage`` 负责，因此用户可
    非连续勾选消息，之后再补选更早的消息；聊天正文不会在这两张状态表中重复落库。
    """

    __tablename__ = "memory_sync_state"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "channel", "contact_id",
            name="uq_memory_sync_state_tenant_channel_contact",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"), index=True)
    channel: Mapped[str] = mapped_column(String(30), index=True)
    contact_id: Mapped[str] = mapped_column(String(120), index=True)
    display_name: Mapped[str] = mapped_column(String(120), default="")
    last_message_key: Mapped[str] = mapped_column(String(160), default="")
    last_message_at: Mapped[int] = mapped_column(Integer, default=0)
    messages_processed: Mapped[int] = mapped_column(Integer, default=0)
    last_status: Mapped[str] = mapped_column(String(24), default="idle")
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_synced_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MemoryIngestedMessage(TimestampMixin, Base):
    """Idempotency ledger for automatic and user-selected messages sent to Mem0.

    Rows retain either a hashed source-message ID or a role-sensitive content hash. Raw chat
    text is never stored here; content hashes bridge automatic and manual ingestion IDs.
    """

    __tablename__ = "memory_ingested_message"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "channel", "contact_id", "message_key",
            name="uq_memory_ingested_message_scope_key",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"), index=True)
    channel: Mapped[str] = mapped_column(String(30), index=True)
    contact_id: Mapped[str] = mapped_column(String(120), index=True)
    message_key: Mapped[str] = mapped_column(String(160))


class MemoryOperation(TimestampMixin, Base):
    """Persistent outbox for recoverable Mem0/SQLite cross-store mutations."""

    __tablename__ = "memory_operation"
    __table_args__ = (UniqueConstraint("operation_id", name="uq_memory_operation_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"), index=True)
    channel: Mapped[str] = mapped_column(String(30), index=True)
    contact_id: Mapped[str] = mapped_column(String(120), index=True)
    operation_id: Mapped[str] = mapped_column(String(80), index=True)
    kind: Mapped[str] = mapped_column(String(24))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
