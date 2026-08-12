from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.admin.session import read_session
from app.db import get_db
from app.models.user import User


@dataclass
class AdminUser:
    id: int
    tenant_id: int
    login: str
    role: str


def _redirect_to_login() -> HTTPException:
    return HTTPException(status_code=303, headers={"Location": "/admin/login"})


def current_admin_user(request: Request, db: Session) -> User | None:
    """回查用户；会话版本号对不上（已登出/被踢/改密）或用户已删 → None。"""
    data = read_session(request.cookies.get("admin_session", ""))
    if not data:
        return None
    user = db.get(User, data.get("id"))
    if user is None or (user.session_version or 0) != data.get("sv"):
        return None
    return user


def admin_user(request: Request, db: Annotated[Session, Depends(get_db)]) -> AdminUser:
    user = current_admin_user(request, db)
    if user is None:
        raise _redirect_to_login()
    # 租户/角色以库为准：降权、换租户即刻生效
    return AdminUser(id=user.id, tenant_id=user.tenant_id, login=user.login, role=user.role)


def require_admin(user: Annotated[AdminUser, Depends(admin_user)]) -> AdminUser:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return user
