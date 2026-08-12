from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.memory import CustomerMemory


def _tenant_memory(db: Session, tenant_id: int, memory_id: int) -> CustomerMemory | None:
    return db.scalar(
        select(CustomerMemory).where(
            CustomerMemory.id == memory_id,
            CustomerMemory.tenant_id == tenant_id,
        )
    )


def list_memories(
    db: Session,
    tenant_id: int,
    *,
    search: str = "",
    memory_type: str = "",
    channel: str = "",
    contact_id: str = "",
    limit: int = 100,
) -> list[CustomerMemory]:
    stmt = select(CustomerMemory).where(CustomerMemory.tenant_id == tenant_id)
    if memory_type:
        stmt = stmt.where(CustomerMemory.memory_type == memory_type)
    if channel:
        stmt = stmt.where(CustomerMemory.channel == channel)
    if contact_id:
        stmt = stmt.where(CustomerMemory.contact_id == contact_id)
    query = search.strip()
    if query:
        pattern = f"%{query}%"
        stmt = stmt.where(or_(
            CustomerMemory.content.ilike(pattern),
            CustomerMemory.contact_id.ilike(pattern),
            CustomerMemory.source.ilike(pattern),
        ))
    stmt = stmt.order_by(
        CustomerMemory.is_pinned.desc(),
        CustomerMemory.importance.desc(),
        CustomerMemory.updated_at.desc(),
        CustomerMemory.id.desc(),
    ).limit(limit)
    return list(db.scalars(stmt))


def create_memory(db: Session, tenant_id: int, **values) -> CustomerMemory:
    memory = CustomerMemory(tenant_id=tenant_id, **values)
    db.add(memory)
    db.commit()
    db.refresh(memory)
    return memory


def update_memory(db: Session, tenant_id: int, memory_id: int, **values) -> CustomerMemory | None:
    memory = _tenant_memory(db, tenant_id, memory_id)
    if memory is None:
        return None
    for field, value in values.items():
        setattr(memory, field, value)
    db.commit()
    db.refresh(memory)
    return memory


def delete_memory(db: Session, tenant_id: int, memory_id: int) -> bool:
    memory = _tenant_memory(db, tenant_id, memory_id)
    if memory is None:
        return False
    db.delete(memory)
    db.commit()
    return True


def memory_stats(db: Session, tenant_id: int) -> dict:
    rows = db.execute(
        select(CustomerMemory.memory_type, func.count(CustomerMemory.id))
        .where(CustomerMemory.tenant_id == tenant_id)
        .group_by(CustomerMemory.memory_type)
    ).all()
    by_type = {str(kind): int(count) for kind, count in rows}
    pinned = db.scalar(
        select(func.count(CustomerMemory.id)).where(
            CustomerMemory.tenant_id == tenant_id,
            CustomerMemory.is_pinned.is_(True),
        )
    ) or 0
    return {"total": sum(by_type.values()), "pinned": int(pinned), "by_type": by_type}


def list_contact_memories(
    db: Session, tenant_id: int, channel: str, contact_id: str, limit: int = 8
) -> list[CustomerMemory]:
    return list_memories(
        db, tenant_id, channel=channel, contact_id=contact_id, limit=limit
    )


def upsert_profile_memory(
    db: Session,
    tenant_id: int,
    channel: str,
    contact_id: str,
    content: str,
) -> CustomerMemory | None:
    normalized = content.strip()
    if not normalized:
        return None
    memory = db.scalar(
        select(CustomerMemory).where(
            CustomerMemory.tenant_id == tenant_id,
            CustomerMemory.channel == channel,
            CustomerMemory.contact_id == contact_id,
            CustomerMemory.source_key == "profile",
        )
    )
    if memory is None:
        memory = CustomerMemory(
            tenant_id=tenant_id,
            channel=channel,
            contact_id=contact_id,
            memory_type="profile",
            content=normalized,
            source="conversation",
            source_key="profile",
            importance=0.8,
        )
        db.add(memory)
    else:
        memory.content = normalized
    db.commit()
    db.refresh(memory)
    return memory
