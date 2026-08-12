import datetime as dt
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.models.conversation import Conversation, Message
from app.models.deal import Deal


def _actually_sent():
    return (
        (Message.delivery_status == "delivered")
        | Message.provenance.in_(("human", "broadcast"))
    )


def _count(db: Session, tenant_id: int, sender: str, day: dt.date | None) -> int:
    q = select(func.count()).select_from(Message).where(
        Message.tenant_id == tenant_id, Message.sender == sender)
    if sender != "customer":
        q = q.where(_actually_sent())
    if day is not None:
        q = q.where(func.date(Message.created_at) == day.isoformat())
    return db.scalar(q) or 0


def today_counts(db: Session, tenant_id: int, today: dt.date | None = None) -> dict:
    today = today or dt.datetime.now(dt.timezone.utc).date()
    r = _count(db, tenant_id, "customer", today)
    a = _count(db, tenant_id, "ai", today)
    h = _count(db, tenant_id, "agent", today)
    return {"received": r, "auto_resolved": a, "handoff": h,
            "auto_rate": min(100, round(a / r * 100)) if r else 0,
            "handoff_rate": min(100, round(h / r * 100)) if r else 0}


def trend(db: Session, tenant_id: int, days: int = 7, today: dt.date | None = None) -> list[dict]:
    today = today or dt.datetime.now(dt.timezone.utc).date()
    out = []
    for i in range(days - 1, -1, -1):
        d = today - dt.timedelta(days=i)
        out.append({"date": d.isoformat()[5:],
                    "received": _count(db, tenant_id, "customer", d),
                    "auto_resolved": _count(db, tenant_id, "ai", d),
                    "handoff": _count(db, tenant_id, "agent", d)})
    return out


def _date_between(col, start: dt.date, end: dt.date):
    return (func.date(col) >= start.isoformat()) & (func.date(col) <= end.isoformat())


def _conv_ids_in_period(db: Session, tenant_id: int, start: dt.date, end: dt.date) -> list[int]:
    return list(db.scalars(select(Conversation.id).where(
        Conversation.tenant_id == tenant_id, _date_between(Conversation.created_at, start, end))))


def new_contacts(db, tenant_id, start, end) -> int:
    first = (select(Conversation.contact_id, func.min(Conversation.created_at).label("f"))
             .where(Conversation.tenant_id == tenant_id)
             .group_by(Conversation.contact_id).subquery())
    return db.scalar(select(func.count()).select_from(first)
                     .where(_date_between(first.c.f, start, end))) or 0


def _effective_conv_ids(db, tenant_id, start, end) -> list[int]:
    in_c = select(Message.conversation_id).where(
        Message.tenant_id == tenant_id, Message.sender == "customer", Message.direction == "in")
    out_c = select(Message.conversation_id).where(
        Message.tenant_id == tenant_id, Message.sender.in_(("ai", "agent")),
        Message.direction == "out", _actually_sent())
    return list(db.scalars(select(Conversation.id).where(
        Conversation.tenant_id == tenant_id, _date_between(Conversation.created_at, start, end),
        Conversation.id.in_(in_c), Conversation.id.in_(out_c))))


def _deals(db, tenant_id, start, end):
    return db.scalars(select(Deal).where(
        Deal.tenant_id == tenant_id, _date_between(Deal.created_at, start, end))).all()


def avg_first_response_sec(db, tenant_id, start, end) -> int:
    total, n = 0, 0
    for cid in _conv_ids_in_period(db, tenant_id, start, end):
        first_in = db.scalar(select(func.min(Message.created_at)).where(
            Message.conversation_id == cid, Message.direction == "in", Message.sender == "customer"))
        if first_in is None:
            continue
        first_out = db.scalar(select(func.min(Message.created_at)).where(
            Message.conversation_id == cid, Message.direction == "out",
            Message.sender.in_(("ai", "agent")), Message.created_at >= first_in,
            _actually_sent()))
        if first_out is None:
            continue
        total += (first_out - first_in).total_seconds(); n += 1
    return round(total / n) if n else 0


def avg_handle_minutes(db, tenant_id, start, end) -> float:
    total, n = 0.0, 0
    for cid in _conv_ids_in_period(db, tenant_id, start, end):
        lo = db.scalar(select(func.min(Message.created_at)).where(Message.conversation_id == cid))
        hi = db.scalar(select(func.max(Message.created_at)).where(Message.conversation_id == cid))
        cnt = db.scalar(select(func.count()).select_from(Message).where(Message.conversation_id == cid)) or 0
        if lo is None or hi is None or cnt < 2:
            continue
        total += (hi - lo).total_seconds() / 60.0; n += 1
    return round(total / n, 1) if n else 0.0


def auto_rate(db, tenant_id, start, end) -> int:
    eff = _effective_conv_ids(db, tenant_id, start, end)
    if not eff:
        return 0
    auto = 0
    for cid in eff:
        agent = db.scalar(select(func.count()).select_from(Message).where(
            Message.conversation_id == cid, Message.sender == "agent",
            Message.direction == "out", _actually_sent())) or 0
        if agent == 0:
            auto += 1
    return round(auto / len(eff) * 100)


def channel_breakdown(db, tenant_id, start, end) -> list[dict]:
    rows = db.execute(select(Conversation.channel, func.count()).where(
        Conversation.tenant_id == tenant_id, _date_between(Conversation.created_at, start, end))
        .group_by(Conversation.channel)).all()
    convs_by_ch = {ch: n for ch, n in rows}
    deals = _deals(db, tenant_id, start, end)
    # 渠道并集 = 期内有会话的 ∪ 期内有成交的：跨期补记的成交（会话是几天前建的、
    # 成交今天才记）其渠道也要出行，否则渠道表各行之和会 < 头条 conversions/gmv。
    channels = set(convs_by_ch) | {d.channel for d in deals}
    out = []
    for channel in sorted(channels):
        dch = [d for d in deals if d.channel == channel]
        out.append({"channel": channel, "convs": convs_by_ch.get(channel, 0),
                    "conversions": len(dch),
                    "gmv_yuan": round(sum(d.amount_cents for d in dch) / 100, 2)})
    return out


def dashboard_metrics(db: Session, tenant_id: int, start: dt.date, end: dt.date) -> dict:
    eff = len(_effective_conv_ids(db, tenant_id, start, end))
    deals = _deals(db, tenant_id, start, end)
    conversions = len(deals)
    gmv_cents = sum(d.amount_cents for d in deals)
    fu = [d for d in deals if d.is_followup]
    return {
        "new_contacts": new_contacts(db, tenant_id, start, end),
        "effective_convs": eff,
        "conversions": conversions,
        "gmv_yuan": round(gmv_cents / 100, 2),
        "conversion_rate": round(conversions / eff * 100, 1) if eff else 0,
        "aov_yuan": round(gmv_cents / conversions / 100, 2) if conversions else 0,
        "avg_handle_min": avg_handle_minutes(db, tenant_id, start, end),
        "auto_rate": auto_rate(db, tenant_id, start, end),
        "avg_first_resp_sec": avg_first_response_sec(db, tenant_id, start, end),
        "churned": max(0, eff - conversions),
        "churn_rate": round(max(0, eff - conversions) / eff * 100, 1) if eff else 0,
        "reactivation_count": len(fu),
        "reactivation_gmv_yuan": round(sum(d.amount_cents for d in fu) / 100, 2),
        "channels": channel_breakdown(db, tenant_id, start, end),
    }


def trend_ext(db: Session, tenant_id: int, days: int = 7, today: dt.date | None = None) -> list[dict]:
    today = today or dt.datetime.now(dt.timezone.utc).date()
    out = []
    for i in range(days - 1, -1, -1):
        d = today - dt.timedelta(days=i)
        deals = _deals(db, tenant_id, d, d)
        out.append({"date": d.isoformat()[5:],
                    "contacts": new_contacts(db, tenant_id, d, d),
                    "conversions": len(deals),
                    "gmv_yuan": round(sum(x.amount_cents for x in deals) / 100, 2)})
    return out
