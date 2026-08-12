"""Read-only, tenant-scoped data and health views for the desktop operations pages."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import delete, desc, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.llm import EMBED_DIM, embedding_model_name
from app.models.conversation import Conversation, Message
from app.models.deal import Deal
from app.models.knowledge import KbChunk, KbDocument
from app.models.memory import CustomerMemory
from app.crud import kb as kb_crud
from app.ops.runtime import OpsTaskRegistry, ops_registry


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _model_gateway() -> dict[str, Any]:
    provider = settings.llm_provider
    chat_model = settings.deepseek_model if provider == "deepseek" else settings.llm_chat_model
    configured = (
        provider == "fake"
        or (provider == "dashscope" and bool(settings.dashscope_api_key))
        or (provider == "deepseek" and bool(settings.deepseek_api_key and settings.dashscope_api_key))
    )
    return {
        "provider": provider,
        "chat_model": chat_model,
        "embedding_model": embedding_model_name(),
        "embedding_dimension": EMBED_DIM,
        "thinking": bool(settings.deepseek_thinking) if provider == "deepseek" else False,
        "configured": configured,
        "mode": "开发模拟" if provider == "fake" else "真实模型",
    }


def ops_overview(db: Session, tenant_id: int, *, registry: OpsTaskRegistry = ops_registry,
                 limit: int = 30) -> dict[str, Any]:
    tenant_id = int(tenant_id)
    events = int(db.scalar(select(func.count(Message.id)).where(Message.tenant_id == tenant_id)) or 0)
    evidence = int(db.scalar(select(func.count(KbChunk.id)).where(KbChunk.tenant_id == tenant_id)) or 0)
    memories = int(db.scalar(select(func.count(CustomerMemory.id)).where(
        CustomerMemory.tenant_id == tenant_id)) or 0)
    tasks = registry.recent(tenant_id, limit=limit)

    recent_messages = db.scalars(
        select(Message).where(Message.tenant_id == tenant_id)
        .order_by(desc(Message.id)).limit(limit)
    ).all()
    recent_evidence = db.execute(
        select(KbChunk, KbDocument.title)
        .join(KbDocument, KbDocument.id == KbChunk.document_id)
        .where(KbChunk.tenant_id == tenant_id, KbDocument.tenant_id == tenant_id)
        .order_by(desc(KbChunk.id)).limit(limit)
    ).all()
    return {
        "stats": {
            "events": events,
            "evidence": evidence,
            "worker_tasks": len(tasks),
            "long_term_memories": memories,
        },
        "events": [{
            "id": item.id,
            "conversation_id": item.conversation_id,
            "direction": item.direction,
            "sender": item.sender,
            "provenance": item.provenance or "",
            "summary": item.text[:180],
            "created_at": _iso(item.created_at),
        } for item in recent_messages],
        "evidence": [{
            "id": chunk.id,
            "document": title,
            "ord": chunk.ord,
            "text": chunk.text[:240],
            "created_at": _iso(chunk.created_at),
        } for chunk, title in recent_evidence],
        "worker_tasks": tasks,
        "model_gateway": _model_gateway(),
    }


def browse_dataset(db: Session, tenant_id: int, dataset: str, *, limit: int = 100) -> dict[str, Any]:
    """Browse a fixed allow-list. Every query contains an explicit tenant predicate."""
    tenant_id, limit = int(tenant_id), min(max(int(limit), 1), 200)
    if dataset == "conversations":
        rows = db.scalars(select(Conversation).where(Conversation.tenant_id == tenant_id)
                          .order_by(desc(Conversation.id)).limit(limit)).all()
        columns = ["id", "channel", "contact_id", "status", "created_at"]
        data = [{"id": row.id, "channel": row.channel, "contact_id": row.contact_id,
                 "status": row.status, "created_at": _iso(row.created_at)} for row in rows]
    elif dataset == "messages":
        rows = db.scalars(select(Message).where(Message.tenant_id == tenant_id)
                          .order_by(desc(Message.id)).limit(limit)).all()
        columns = ["id", "conversation_id", "direction", "sender", "provenance", "text", "created_at"]
        data = [{"id": row.id, "conversation_id": row.conversation_id,
                 "direction": row.direction, "sender": row.sender,
                 "provenance": row.provenance or "", "text": row.text[:500],
                 "created_at": _iso(row.created_at)} for row in rows]
    elif dataset == "memories":
        rows = db.scalars(select(CustomerMemory).where(CustomerMemory.tenant_id == tenant_id)
                          .order_by(desc(CustomerMemory.id)).limit(limit)).all()
        columns = ["id", "channel", "contact_id", "memory_type", "content", "importance", "pinned", "updated_at"]
        data = [{"id": row.id, "channel": row.channel, "contact_id": row.contact_id,
                 "memory_type": row.memory_type, "content": row.content[:500],
                 "importance": row.importance, "pinned": row.is_pinned,
                 "updated_at": _iso(row.updated_at)} for row in rows]
    elif dataset == "knowledge":
        rows = db.scalars(select(KbDocument).where(KbDocument.tenant_id == tenant_id)
                          .order_by(desc(KbDocument.id)).limit(limit)).all()
        columns = ["id", "title", "source_type", "status", "created_at"]
        data = [{"id": row.id, "title": row.title, "source_type": row.source_type,
                 "status": row.status, "created_at": _iso(row.created_at)} for row in rows]
    else:
        raise ValueError(f"不支持的数据集：{dataset}")
    return {"dataset": dataset, "columns": columns, "rows": data, "count": len(data)}


def delete_dataset_rows(
    db: Session,
    tenant_id: int,
    dataset: str,
    row_ids: list[int],
    *,
    memory_service: Any = None,
) -> dict[str, Any]:
    """Delete allow-listed rows with tenant isolation and domain-specific cleanup."""
    tenant_id = int(tenant_id)
    ids = list(dict.fromkeys(int(value) for value in row_ids if int(value) > 0))[:200]
    if not ids:
        return {"dataset": dataset, "deleted": 0}
    if dataset == "conversations":
        owned_ids = list(db.scalars(select(Conversation.id).where(
            Conversation.tenant_id == tenant_id, Conversation.id.in_(ids),
        )))
        if owned_ids:
            db.execute(delete(Deal).where(
                Deal.tenant_id == tenant_id, Deal.conversation_id.in_(owned_ids),
            ))
            db.execute(delete(Message).where(
                Message.tenant_id == tenant_id, Message.conversation_id.in_(owned_ids),
            ))
            db.execute(delete(Conversation).where(
                Conversation.tenant_id == tenant_id, Conversation.id.in_(owned_ids),
            ))
            db.commit()
        deleted_count = len(owned_ids)
    elif dataset == "messages":
        owned_ids = list(db.scalars(select(Message.id).where(
            Message.tenant_id == tenant_id, Message.id.in_(ids),
        )))
        if owned_ids:
            db.execute(delete(Message).where(
                Message.tenant_id == tenant_id, Message.id.in_(owned_ids),
            ))
            db.commit()
        deleted_count = len(owned_ids)
    elif dataset == "memories":
        if memory_service is None:
            from app.memory.customer_service import get_customer_memory_service

            memory_service = get_customer_memory_service()
        deleted_count = sum(
            bool(memory_service.delete_memory(
                db, tenant_id=tenant_id, memory_id=memory_id,
            ))
            for memory_id in ids
        )
    elif dataset == "knowledge":
        owned_ids = list(db.scalars(select(KbDocument.id).where(
            KbDocument.tenant_id == tenant_id, KbDocument.id.in_(ids),
        )))
        for document_id in owned_ids:
            kb_crud.delete_document(db, tenant_id, document_id)
        deleted_count = len(owned_ids)
    else:
        raise ValueError(f"不支持的数据集：{dataset}")
    return {"dataset": dataset, "deleted": int(deleted_count)}


def run_functional_checks(db: Session, tenant_id: int) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    try:
        db.scalar(select(func.count(Conversation.id)).where(Conversation.tenant_id == int(tenant_id)))
        checks.append({"name": "租户数据库", "status": "passed", "detail": "连接正常，租户过滤可执行"})
    except Exception as exc:  # noqa: BLE001
        checks.append({"name": "租户数据库", "status": "failed", "detail": type(exc).__name__})

    gateway = _model_gateway()
    checks.append({
        "name": "模型网关",
        "status": "passed" if gateway["configured"] else "warning",
        "detail": f"{gateway['provider']} · {gateway['chat_model']} · {gateway['embedding_model']}",
    })
    try:
        from app.memory import get_memory_workbench_service
        health = get_memory_workbench_service().gateway.health()
        live = health.get("status") == "ok"
        checks.append({"name": "记忆引擎", "status": "passed" if live else "warning",
                       "detail": str(health.get("mode") or health.get("detail") or "离线")[:180]})
    except Exception as exc:  # noqa: BLE001
        checks.append({"name": "记忆引擎", "status": "warning", "detail": type(exc).__name__})
    return checks
