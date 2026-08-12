from dataclasses import dataclass
from sqlalchemy.orm import Session
from app.models.wecom import WeComConfig
from app.security.field_crypto import encrypt_field, decrypt_field


@dataclass
class WeComCfg:
    tenant_id: int
    corp_id: str
    secret: str
    callback_token: str
    encoding_aes_key: str
    open_kfid: str
    sync_cursor: str
    enabled: bool


def set_config(db: Session, tenant_id: int, *, corp_id, secret, callback_token,
               encoding_aes_key, open_kfid) -> None:
    row = db.get(WeComConfig, tenant_id) or WeComConfig(tenant_id=tenant_id)
    row.corp_id = corp_id
    row.secret_enc = encrypt_field(secret)
    row.callback_token = callback_token
    row.aeskey_enc = encrypt_field(encoding_aes_key)
    row.open_kfid = open_kfid
    db.add(row); db.commit()


def get_config(db: Session, tenant_id: int) -> WeComCfg | None:
    row = db.get(WeComConfig, tenant_id)
    if row is None:
        return None
    return WeComCfg(tenant_id=row.tenant_id, corp_id=row.corp_id,
                   secret=decrypt_field(row.secret_enc), callback_token=row.callback_token,
                   encoding_aes_key=decrypt_field(row.aeskey_enc), open_kfid=row.open_kfid,
                   sync_cursor=row.sync_cursor, enabled=row.enabled)


def update_cursor(db: Session, tenant_id: int, cursor: str) -> None:
    row = db.get(WeComConfig, tenant_id)
    if row is not None:
        row.sync_cursor = cursor; db.add(row); db.commit()
