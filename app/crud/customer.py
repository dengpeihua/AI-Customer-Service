from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.conversation import Conversation, Message
from app.models.customer import CustomerProfile


def get_profile(db: Session, tenant_id: int, channel: str, contact_id: str) -> CustomerProfile | None:
    return db.scalar(
        select(CustomerProfile).where(
            CustomerProfile.tenant_id == tenant_id,
            CustomerProfile.channel == channel,
            CustomerProfile.contact_id == contact_id,
        )
    )


def upsert_profile(db: Session, tenant_id: int, channel: str, contact_id: str,
                   summary: str, tags: list) -> CustomerProfile:
    prof = get_profile(db, tenant_id, channel, contact_id)
    if prof is None:
        prof = CustomerProfile(tenant_id=tenant_id, channel=channel, contact_id=contact_id)
        db.add(prof)
    prof.summary = summary
    prof.tags = tags
    try:
        db.commit()
    except IntegrityError:                       # 并发下另一个写入者已抢先建了同键行
        db.rollback()
        prof = get_profile(db, tenant_id, channel, contact_id)
        if prof is None:
            raise
        prof.summary = summary
        prof.tags = tags
        db.commit()
    db.refresh(prof)
    return prof


def recent_customer_questions(db: Session, tenant_id: int, channel: str, contact_id: str,
                              limit: int = 10) -> list[str]:
    rows = db.scalars(
        select(Message.text)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(
            Conversation.tenant_id == tenant_id,
            Conversation.channel == channel,
            Conversation.contact_id == contact_id,
            Message.sender == "customer",
        )
        .order_by(Message.id.desc())
        .limit(limit)
    ).all()
    return list(rows)
