from pydantic import BaseModel, Field


def _mask(s: str) -> str:
    return ("****" + s[-4:]) if len(s) > 4 else "****"


class WeComConfigIn(BaseModel):
    corp_id: str = Field(min_length=1)
    secret: str = Field(min_length=1)
    callback_token: str = Field(min_length=1)
    encoding_aes_key: str = Field(min_length=43, max_length=43)
    open_kfid: str = Field(min_length=1)


class WeComConfigOut(BaseModel):
    corp_id: str
    secret_masked: str
    encoding_aes_key_masked: str
    open_kfid: str
    enabled: bool

    @classmethod
    def from_cfg(cls, cfg) -> "WeComConfigOut":
        return cls(corp_id=cfg.corp_id, secret_masked=_mask(cfg.secret),
                   encoding_aes_key_masked="****",     # 密钥材料不回显任何位
                   open_kfid=cfg.open_kfid, enabled=cfg.enabled)
