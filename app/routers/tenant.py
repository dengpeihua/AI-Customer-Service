from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import settings
from app.crud.tenant import create_tenant
from app.crud.user import create_user
from app.db import get_db

router = APIRouter(prefix="/v1/tenants", tags=["tenant"])


class TenantCreate(BaseModel):
    name: str
    admin_login: str
    admin_password: str = Field(max_length=72)


class TenantOut(BaseModel):
    id: int
    name: str
    admin_user_id: int


@router.post("", response_model=TenantOut)
def bootstrap_tenant(
    body: TenantCreate,
    db: Annotated[Session, Depends(get_db)],
    x_bootstrap_token: Annotated[str | None, Header()] = None,
):
    if x_bootstrap_token != settings.bootstrap_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad bootstrap token")
    tenant = create_tenant(db, name=body.name)
    admin = create_user(
        db,
        tenant_id=tenant.id,
        login=body.admin_login,
        password=body.admin_password,
        role="admin",
    )
    return TenantOut(id=tenant.id, name=tenant.name, admin_user_id=admin.id)
