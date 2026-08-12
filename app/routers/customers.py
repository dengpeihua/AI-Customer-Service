from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.crud.customer import get_profile, recent_customer_questions
from app.db import get_db
from app.deps import CurrentUser

router = APIRouter(prefix="/v1/customers", tags=["customers"])


class ProfileOut(BaseModel):
    channel: str
    contact_id: str
    summary: str
    tags: list[str]
    recent_questions: list[str]
    updated_at: str | None = None


@router.get("/profile", response_model=ProfileOut)
def get_customer_profile(channel: str, contact_id: str, user: CurrentUser,
                         db: Annotated[Session, Depends(get_db)]):
    prof = get_profile(db, user.tenant_id, channel, contact_id)
    return ProfileOut(
        channel=channel, contact_id=contact_id,
        summary=(prof.summary if prof else ""),
        tags=(prof.tags if prof else []),
        recent_questions=recent_customer_questions(db, user.tenant_id, channel, contact_id),
        updated_at=(prof.updated_at.isoformat() if prof and prof.updated_at else None),
    )
