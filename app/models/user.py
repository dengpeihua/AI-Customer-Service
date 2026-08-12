from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import TimestampMixin


class User(TimestampMixin, Base):
    __tablename__ = "user_account"
    __table_args__ = (
        UniqueConstraint("tenant_id", "login", name="uq_user_tenant_login"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"), index=True)
    login: Mapped[str] = mapped_column(String(100))
    pwd_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), default="agent")  # admin | agent
    # 递增即让该用户已签发的所有后台会话立刻失效（登出/踢下线/改密码）
    session_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
