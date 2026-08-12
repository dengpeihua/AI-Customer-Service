from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import CurrentUser
from app.dialog.delivery import get_delivery_status, message_was_delivered, update_delivery_status
from app.models.conversation import Conversation, Message
from app.schemas.conversation import ConversationDetail, ConversationSummary, MessageOut

router = APIRouter(prefix="/v1/conversations", tags=["conversations"])


class DeliveryUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    delivery_status: Literal["sending", "delivered", "failed", "draft"]
    attempt_id: str | None = None


class DeliveryUpdateOut(BaseModel):
    message_id: int
    delivery_status: str
    changed: bool
    attempt_id: str | None = None
    recovered_expired_lease: bool = False


@router.patch("/messages/{message_id}/delivery", response_model=DeliveryUpdateOut)
def mark_message_delivery(
    message_id: int,
    body: DeliveryUpdateIn,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
):
    message, changed, recovered_expired_lease = update_delivery_status(
        db,
        tenant_id=user.tenant_id,
        message_id=message_id,
        delivery_status=body.delivery_status,
        attempt_id=body.attempt_id,
    )
    if message is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "message not found")
    return DeliveryUpdateOut(
        message_id=message.id,
        delivery_status=get_delivery_status(message),
        changed=changed,
        attempt_id=message.delivery_attempt_id,
        recovered_expired_lease=recovered_expired_lease,
    )


@router.get("", response_model=list[ConversationSummary])
def list_conversations(
    user: CurrentUser, db: Annotated[Session, Depends(get_db)],
    limit: int = 50, offset: int = 0,
):
    # 子查询：每会话最新消息 id（用于"最近活动优先"排序）
    sub = (
        select(Message.conversation_id.label("cid"), func.max(Message.id).label("last_id"))
        .where(Message.tenant_id == user.tenant_id)
        .group_by(Message.conversation_id)
        .subquery()
    )
    rows = db.execute(
        select(Conversation, sub.c.last_id)
        .where(Conversation.tenant_id == user.tenant_id)
        .outerjoin(sub, sub.c.cid == Conversation.id)
        .order_by(func.coalesce(sub.c.last_id, 0).desc())
        .limit(limit).offset(offset)
    ).all()
    out: list[ConversationSummary] = []
    for conv, _last_id in rows:
        visible_messages = [
            message for message in db.scalars(
                select(Message).where(Message.conversation_id == conv.id).order_by(Message.id)
            ).all()
            if message_was_delivered(message)
        ]
        last = visible_messages[-1] if visible_messages else None
        count = len(visible_messages)
        out.append(ConversationSummary(
            id=conv.id, contact_id=conv.contact_id, channel=conv.channel, status=conv.status,
            last_text=(last.text if last else ""),
            last_at=(last.created_at if last else conv.created_at),
            message_count=count,
        ))
    return out


@router.get("/{conv_id}", response_model=ConversationDetail)
def get_conversation(
    conv_id: int, user: CurrentUser, db: Annotated[Session, Depends(get_db)],
):
    conv = db.get(Conversation, conv_id)
    if conv is None or conv.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    msgs = [
        message for message in db.scalars(
        select(Message).where(Message.conversation_id == conv.id).order_by(Message.id)
        ).all()
        if message_was_delivered(message)
    ]
    return ConversationDetail(
        id=conv.id, contact_id=conv.contact_id, channel=conv.channel,
        status=conv.status, created_at=conv.created_at,
        messages=[MessageOut(
            id=m.id,
            direction=m.direction,
            sender=m.sender,
            provenance=m.provenance,
            delivery_status=get_delivery_status(m) or None,
            text=m.text,
            created_at=m.created_at,
        ) for m in msgs],
    )
