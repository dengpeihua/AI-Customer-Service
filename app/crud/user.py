from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.user import User
from app.security import hash_password


def create_user(
    db: Session, tenant_id: int, login: str, password: str, role: str = "agent"
) -> User:
    u = User(tenant_id=tenant_id, login=login, pwd_hash=hash_password(password), role=role)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def get_user_by_login(db: Session, tenant_id: int, login: str) -> User | None:
    return db.scalar(
        select(User).where(User.tenant_id == tenant_id, User.login == login)
    )


def bump_session_version(db: Session, user: User) -> User:
    """撤销该用户已签发的全部后台会话（登出、踢下线、改密码后调用）。"""
    user.session_version = (user.session_version or 0) + 1
    db.commit()
    db.refresh(user)
    return user
