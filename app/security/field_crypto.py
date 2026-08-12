"""字段级加密（AES-GCM）。用于把 wecom_config 的 secret/aeskey 密文落库。
密钥取 settings.field_enc_key(base64 32B)；未配则从 jwt_secret 派生并告警（dev 可跑，prod 必配）。"""
from __future__ import annotations
import base64, hashlib, logging, os
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from app.config import settings

_log = logging.getLogger(__name__)
_KEY: bytes | None = None                 # 模块级缓存：首次计算后复用，避免重复告警


def _reset_key_cache() -> None:
    """测试用：清空缓存，便于运行期切换 settings.field_enc_key 后重新派生。"""
    global _KEY
    _KEY = None


def _key() -> bytes:
    global _KEY
    if _KEY is not None:
        return _KEY
    if settings.field_enc_key:
        k = base64.b64decode(settings.field_enc_key)
        if len(k) != 32:
            raise ValueError("field_enc_key 必须是 base64 编码的 32 字节")
        _KEY = k
    else:
        _log.warning("field_enc_key 未配置，从 jwt_secret 派生（生产环境请配置独立强密钥）")
        _KEY = hashlib.sha256(settings.jwt_secret.encode("utf-8")).digest()
    return _KEY


def encrypt_field(plaintext: str) -> str:
    nonce = os.urandom(12)
    ct = AESGCM(_key()).encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ct).decode("ascii")


def decrypt_field(enc: str) -> str:
    raw = base64.b64decode(enc)
    return AESGCM(_key()).decrypt(raw[:12], raw[12:], None).decode("utf-8")
