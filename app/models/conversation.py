import datetime as dt

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db import Base
from app.models.base import TimestampMixin


class Conversation(TimestampMixin, Base):
    __tablename__ = "conversation"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"), index=True)
    channel: Mapped[str] = mapped_column(String(30))  # wechat_personal|wecom
    contact_id: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default="active")  # active|handoff|closed
    assignee_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("user_account.id"), nullable=True
    )


class Message(TimestampMixin, Base):
    __tablename__ = "message"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "source_message_id",
            name="uq_message_source_per_tenant",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"), index=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversation.id"), index=True)
    direction: Mapped[str] = mapped_column(String(10))  # in|out
    sender: Mapped[str] = mapped_column(String(20))  # customer|ai|agent
    # provenance = 权威来源标记（供挂件聊天页按字段判定，不再靠事后文本猜测）。
    # customer=客户 | ai=AI自动 | handoff=转人工草稿 | human=人工手打 | broadcast=群发
    provenance: Mapped[str | None] = mapped_column(String(16), nullable=True)
    delivery_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    delivery_attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    delivery_recovered_attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    delivery_claimed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # 渠道侧入站消息 id：用于阻止同一轮询事件被重复提交、重复调用 LLM 和重复回复。
    source_message_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
