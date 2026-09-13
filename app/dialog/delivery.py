from __future__ import annotations

import datetime as dt
from typing import Literal

from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from app.models.conversation import Message

DeliveryStatus = Literal["pending", "sending", "delivered", "failed", "draft"]
DELIVERY_STATUSES = {"pending", "sending", "delivered", "failed", "draft"}


def get_delivery_status(message: Message) -> str:
    if message.delivery_status:
        return str(message.delivery_status)
    meta = message.meta if isinstance(message.meta, dict) else {}
    return str(meta.get("delivery_status") or "")


def message_was_delivered(message: Message) -> bool:
    """Whether a message is safe to present/use as an actual conversation turn."""
    if message.direction != "out":
        return True
    if message.provenance in {"human", "broadcast"}:
        return True
    return get_delivery_status(message) == "delivered"


def message_belongs_in_agent_history(message: Message) -> bool:
    """Keep internal handoff boundaries while excluding unsent assistant prose."""
    if message.provenance == "handoff":
        return True
    return message_was_delivered(message)


def update_delivery_status(
    db: Session,
    *,
    tenant_id: int,
    message_id: int,
    delivery_status: DeliveryStatus,
    attempt_id: str | None = None,
    now: dt.datetime | None = None,
    lease_seconds: float = 300.0,
) -> tuple[Message | None, bool, bool]:
    if delivery_status not in DELIVERY_STATUSES:
        raise ValueError(f"unsupported delivery status: {delivery_status}")
    message = db.get(Message, message_id)
    if message is None or message.tenant_id != tenant_id or message.direction != "out":
        return None, False, False

    attempt_id = str(attempt_id or "").strip() or None
    if delivery_status in {"sending", "delivered", "failed"} and not attempt_id:
        raise ValueError(f"attempt_id is required for {delivery_status}")
    now = now or dt.datetime.now(dt.timezone.utc)
    lease_cutoff = now - dt.timedelta(seconds=max(1.0, float(lease_seconds)))
    prior_status = get_delivery_status(message)
    prior_attempt = str(message.delivery_attempt_id or "")
    prior_recovered_attempt = str(message.delivery_recovered_attempt_id or "")
    prior_claimed_at = message.delivery_claimed_at
    if prior_claimed_at is not None and prior_claimed_at.tzinfo is None:
        prior_claimed_at = prior_claimed_at.replace(tzinfo=dt.timezone.utc)
    taking_over_expired_lease = bool(
        delivery_status == "sending"
        and prior_status == "sending"
        and prior_attempt != attempt_id
        and (prior_claimed_at is None or prior_claimed_at < lease_cutoff)
    )
    recovered_expired_lease = bool(
        taking_over_expired_lease
        or (delivery_status == "sending" and prior_recovered_attempt == attempt_id)
    )
    next_meta = {**(message.meta or {}), "delivery_status": delivery_status}
    statement = update(Message).where(
        Message.id == message_id,
        Message.tenant_id == tenant_id,
        Message.direction == "out",
    )
    if delivery_status == "sending":
        # 只有一个调用方能把可恢复状态抢占为 sending；其余并发恢复者 changed=False，禁止发抖音私信。
        statement = statement.where(or_(
            Message.delivery_status.is_(None),
            Message.delivery_status.in_(("pending", "failed", "draft")),
            (Message.delivery_status == "sending")
            & (Message.delivery_attempt_id == attempt_id),
            (Message.delivery_status == "sending")
            & or_(
                Message.delivery_claimed_at.is_(None),
                Message.delivery_claimed_at < lease_cutoff,
            ),
        ))
    elif delivery_status in {"delivered", "failed"}:
        # 最终回执只接受当前持有发送权的调用方；delivered 一旦写入，任何晚到状态都匹配不到。
        statement = statement.where(
            Message.delivery_status == "sending",
            Message.delivery_attempt_id == attempt_id,
        )
    elif delivery_status == "draft":
        statement = statement.where(or_(
            Message.delivery_status.is_(None),
            Message.delivery_status.in_(("pending", "failed", "draft")),
        ))
    values = {"delivery_status": delivery_status, "meta": next_meta}
    if attempt_id:
        values["delivery_attempt_id"] = attempt_id
    if delivery_status == "sending":
        values["delivery_claimed_at"] = now
        if taking_over_expired_lease:
            values["delivery_recovered_attempt_id"] = attempt_id
    result = db.execute(
        statement.values(**values),
        execution_options={"synchronize_session": False},
    )
    changed = bool(result.rowcount)
    db.commit()
    db.expire(message)
    db.refresh(message)
    return message, changed, bool(changed and recovered_expired_lease)
