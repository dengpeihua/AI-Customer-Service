import datetime as dt

import bcrypt
import jwt

from app.config import settings


# bcrypt 只认前 72 字节，且 5.x 起对超长输入直接抛 ValueError（不再静默截断）。
# 中文密码 1 字≈3 字节，24 字就超 72 字节——不预截会 500。哈希与校验必须用同一套截断，否则对不上。
def _bcrypt_bytes(pwd: str) -> bytes:
    return pwd.encode("utf-8")[:72]


def hash_password(pwd: str) -> str:
    return bcrypt.hashpw(_bcrypt_bytes(pwd), bcrypt.gensalt()).decode()


def verify_password(pwd: str, pwd_hash: str) -> bool:
    return bcrypt.checkpw(_bcrypt_bytes(pwd), pwd_hash.encode())


def create_access_token(sub: str, tenant_id: int, role: str) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "sub": sub,
        "tenant_id": tenant_id,
        "role": role,
        "iat": now,
        "exp": now + dt.timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_alg)


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_alg])
