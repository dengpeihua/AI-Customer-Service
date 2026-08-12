"""企业微信 WXBizMsgCrypt：SHA1 验签 + AES-256-CBC 收发消息加解密。
明文体 = random(16) + msg_len(4,大端) + msg + receiveid；PKCS7 按 32 字节块填充。"""
from __future__ import annotations
import base64, hashlib, hmac, os, struct
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_BLOCK = 32


def _aes_key(encoding_aes_key: str) -> bytes:
    return base64.b64decode(encoding_aes_key + "=")


def sign(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    arr = "".join(sorted([token, str(timestamp), str(nonce), encrypt]))
    return hashlib.sha1(arr.encode("utf-8")).hexdigest()


def verify_signature(token, timestamp, nonce, encrypt, signature) -> bool:
    expected = sign(token, timestamp, nonce, encrypt)
    return hmac.compare_digest(expected.encode("utf-8"), str(signature).encode("utf-8"))


def decrypt(encoding_aes_key: str, encrypt: str) -> tuple[str, str]:
    key = _aes_key(encoding_aes_key)
    dec = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
    plain = dec.update(base64.b64decode(encrypt)) + dec.finalize()
    pad = plain[-1]
    if pad < 1 or pad > 32:
        raise ValueError("invalid PKCS7 padding")
    plain = plain[:-pad]                              # 去 PKCS7 填充
    content = plain[16:]                             # 去前 16 随机字节
    msg_len = struct.unpack(">I", content[:4])[0]
    msg = content[4:4 + msg_len].decode("utf-8")
    receiveid = content[4 + msg_len:].decode("utf-8")
    return msg, receiveid


def encrypt(encoding_aes_key: str, plaintext: str, receiveid: str) -> str:
    key = _aes_key(encoding_aes_key)
    raw = os.urandom(16) + struct.pack(">I", len(plaintext.encode("utf-8"))) \
        + plaintext.encode("utf-8") + receiveid.encode("utf-8")
    pad = _BLOCK - (len(raw) % _BLOCK)
    raw += bytes([pad]) * pad
    enc = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    return base64.b64encode(enc.update(raw) + enc.finalize()).decode("ascii")
