from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import TimestampMixin


class Deal(TimestampMixin, Base):
    __tablename__ = "deal"
    __table_args__ = (UniqueConstraint("conversation_id", name="uq_deal_conversation"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"), index=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversation.id"), index=True)
    contact_id: Mapped[str] = mapped_column(String(120))
    channel: Mapped[str] = mapped_column(String(30))
    amount_cents: Mapped[int] = mapped_column(default=0)
    is_followup: Mapped[bool] = mapped_column(default=False)
    note: Mapped[str] = mapped_column(Text, default="")
