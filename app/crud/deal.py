from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.conversation import Conversation
from app.models.deal import Deal


def get_deal_for_conversation(db: Session, tenant_id: int, conversation_id: int) -> Deal | None:
    return db.scalar(
        select(Deal).where(Deal.tenant_id == tenant_id, Deal.conversation_id == conversation_id)
    )


def record_deal(db: Session, tenant_id: int, conversation_id: int, *,
                amount_cents: int, is_followup: bool, note: str) -> Deal:
    conv = db.get(Conversation, conversation_id)
    if conv is None or conv.tenant_id != tenant_id:
        raise ValueError("conversation not found for tenant")
    deal = get_deal_for_conversation(db, tenant_id, conversation_id)
    if deal is None:
        deal = Deal(tenant_id=tenant_id, conversation_id=conversation_id,
                    contact_id=conv.contact_id, channel=conv.channel)
        db.add(deal)
    deal.amount_cents = int(amount_cents)
    deal.is_followup = bool(is_followup)
    deal.note = note or ""
    db.commit()
    return deal
