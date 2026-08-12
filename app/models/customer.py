import datetime as dt

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db import Base
from app.models.base import TimestampMixin


class CustomerProfile(TimestampMixin, Base):
    __tablename__ = "customer_profile"
    __table_args__ = (
        UniqueConstraint("tenant_id", "channel", "contact_id",
                         name="uq_customer_profile_tenant_channel_contact"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"), index=True)
    channel: Mapped[str] = mapped_column(String(30))
    contact_id: Mapped[str] = mapped_column(String(120))
    summary: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
