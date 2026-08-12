import datetime as dt

from sqlalchemy import (Boolean, DateTime, ForeignKey, String, Text,
                        UniqueConstraint, func)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import TimestampMixin


class Tag(TimestampMixin, Base):
    """客户标签（照 WeiClaw 自定义标签体系自研）：带颜色/描述/轻量分组/AI 托管开关。

    - description 会喂进 AI prompt（tag-for-grounding，对齐 WeiClaw 的标签描述元数据）。
    - ai_muted=True：打了此标签的客户不自动回复、直接转人工（对齐 WeiClaw 按标签的 setAiEnabled/黑名单）。
    """
    __tablename__ = "tag"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_tag_tenant_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"), index=True)
    name: Mapped[str] = mapped_column(String(60))
    color: Mapped[str] = mapped_column(String(20), default="")        # 如 "#4f8cff"，UI 展示用
    description: Mapped[str] = mapped_column(Text, default="")         # 供 AI grounding
    group_name: Mapped[str] = mapped_column(String(60), default="")   # 轻量分组（WeiClaw customTagGroup 简化）
    ai_muted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")


class CustomerTag(Base):
    """客户↔标签关联。客户身份用 (channel, contact_id)，与 customer_profile 同源
    （不强制 customer_profile 已存在，可先打标签）。"""
    __tablename__ = "customer_tag"
    __table_args__ = (
        UniqueConstraint("tenant_id", "tag_id", "channel", "contact_id",
                         name="uq_customer_tag_unique"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"), index=True)
    tag_id: Mapped[int] = mapped_column(ForeignKey("tag.id"), index=True)
    channel: Mapped[str] = mapped_column(String(30))
    contact_id: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
