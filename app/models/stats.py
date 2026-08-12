import datetime as dt

from sqlalchemy import Date, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class StatDaily(Base):
    __tablename__ = "stat_daily"

    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"), primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    received: Mapped[int] = mapped_column(default=0)
    auto_resolved: Mapped[int] = mapped_column(default=0)
    handoff: Mapped[int] = mapped_column(default=0)
