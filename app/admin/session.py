from itsdangerous import URLSafeTimedSerializer, BadData
from app.config import settings

_MAX_AGE = 12 * 3600


def _ser() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.jwt_secret, salt="admin-session")


def make_session(user) -> str:
    """cookie 里只放 id + 会话版本号；租户/角色每次请求以库为准，不信 cookie。"""
    return _ser().dumps({"id": user.id, "sv": user.session_version or 0})


def read_session(token: str) -> dict | None:
    if not token:
        return None
    try:
        return _ser().loads(token, max_age=_MAX_AGE)
    except BadData:
        return None
