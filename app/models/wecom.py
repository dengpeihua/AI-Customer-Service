from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base


class WeComConfig(Base):
    __tablename__ = "wecom_config"
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenant.id"), primary_key=True)
    corp_id: Mapped[str] = mapped_column(String(64))
    secret_enc: Mapped[str] = mapped_column(Text)          # 密文
    callback_token: Mapped[str] = mapped_column(String(64))
    aeskey_enc: Mapped[str] = mapped_column(Text)          # 密文
    open_kfid: Mapped[str] = mapped_column(String(64))
    sync_cursor: Mapped[str] = mapped_column(String(256), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
