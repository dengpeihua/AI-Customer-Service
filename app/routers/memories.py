from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app.crud import memory as memory_crud
from app.db import get_db
from app.deps import CurrentUser
from app.memory import get_memory_workbench_service
from app.memory.customer_service import get_customer_memory_service
from app.memory.mem0_gateway import Mem0Unavailable
from app.models.memory import CustomerMemory
from app.ops import ops_overview, ops_registry


router = APIRouter(prefix="/v1/memories", tags=["memories"])
MemoryKind = Literal["profile", "fact", "preference", "need", "commitment", "note"]


class MemoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    channel: str = Field(min_length=1, max_length=30)
    contact_id: str = Field(min_length=1, max_length=120)
    memory_type: MemoryKind = "note"
    content: str = Field(min_length=1, max_length=2000)
    source: Literal["manual"] = "manual"
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    is_pinned: bool = False


class MemoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    channel: str | None = Field(default=None, min_length=1, max_length=30)
    contact_id: str | None = Field(default=None, min_length=1, max_length=120)
    memory_type: MemoryKind | None = None
    content: str | None = Field(default=None, min_length=1, max_length=2000)
    importance: float | None = Field(default=None, ge=0.0, le=1.0)
    is_pinned: bool | None = None

    @model_validator(mode="after")
    def reject_explicit_nulls(self):
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("更新字段不能为 null")
        return self


class MemoryOut(BaseModel):
    id: int
    channel: str
    contact_id: str
    memory_type: str
    content: str
    source: str
    source_key: str | None
    importance: float
    is_pinned: bool
    created_at: str | None
    updated_at: str | None


class MemoryStatsOut(BaseModel):
    total: int
    pinned: int
    by_type: dict[str, int]


class MemoryDecisionDeleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[int] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def require_positive_unique_ids(self):
        if any(value <= 0 for value in self.ids):
            raise ValueError("治理决策 ID 必须为正整数")
        self.ids = list(dict.fromkeys(self.ids))
        return self


class ConversationMemoryMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    key: str = Field(min_length=1, max_length=160)
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)
    timestamp: int = Field(default=0, ge=0)


class ConversationIngestIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    channel: str = Field(min_length=1, max_length=30)
    contact_id: str = Field(min_length=1, max_length=120)
    display_name: str = Field(default="", max_length=120)
    messages: list[ConversationMemoryMessage] = Field(min_length=1, max_length=500)


class MemoryRecallIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    channel: str = Field(min_length=1, max_length=30)
    contact_id: str = Field(min_length=1, max_length=120)
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=8, ge=1, le=50)


class MemoryBenchmarkRecallIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    case_index: int = Field(default=0, ge=0, le=9)
    question_index: int = Field(default=0, ge=0)
    query: str = Field(default="", max_length=1000)
    limit: int = Field(default=8, ge=1, le=50)
    session_index: int = Field(default=1, ge=1)
    across_sessions: bool = True


def _out(memory: CustomerMemory) -> MemoryOut:
    return MemoryOut(
        id=memory.id,
        channel=memory.channel,
        contact_id=memory.contact_id,
        memory_type=memory.memory_type,
        content=memory.content,
        source=memory.source,
        source_key=memory.source_key,
        importance=memory.importance,
        is_pinned=memory.is_pinned,
        created_at=memory.created_at.isoformat() if memory.created_at else None,
        updated_at=memory.updated_at.isoformat() if memory.updated_at else None,
    )


@router.get("/contacts")
def get_memory_contacts(
    user: CurrentUser, db: Annotated[Session, Depends(get_db)]
):
    return get_customer_memory_service().list_contacts(db, tenant_id=user.tenant_id)


@router.get("/profile")
def get_long_term_profile(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    channel: str = Query(min_length=1, max_length=30),
    contact_id: str = Query(min_length=1, max_length=120),
):
    try:
        return get_customer_memory_service().list_profile(
            db, tenant_id=user.tenant_id, channel=channel, contact_id=contact_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/conversations/ingest")
def ingest_memory_conversation(
    body: ConversationIngestIn,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
):
    task_id = ops_registry.start(user.tenant_id, "memory.ingest", "从真实聊天提取长期记忆")
    try:
        result = get_customer_memory_service().ingest_conversation(
            db, tenant_id=user.tenant_id, channel=body.channel,
            contact_id=body.contact_id, display_name=body.display_name,
            messages=[message.model_dump() for message in body.messages],
        )
        ops_registry.finish(
            task_id, status="completed",
            detail=f"处理 {result['processed_messages']} 条，形成 {result['memory_count']} 条记忆",
        )
        return result
    except Mem0Unavailable as exc:
        ops_registry.finish(task_id, status="failed", detail="Mem0Unavailable")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        ops_registry.finish(task_id, status="failed", detail="ValueError")
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        ops_registry.finish(task_id, status="failed", detail=type(exc).__name__)
        raise


@router.post("/recall")
def recall_memory(body: MemoryRecallIn, user: CurrentUser):
    task_id = ops_registry.start(user.tenant_id, "memory.recall", "召回真实客户长期记忆")
    try:
        result = get_customer_memory_service().recall(
            tenant_id=user.tenant_id, channel=body.channel, contact_id=body.contact_id,
            query=body.query, limit=body.limit,
        )
        ops_registry.finish(task_id, status="completed", detail=f"命中 {result['result_count']} 条")
        return result
    except Mem0Unavailable as exc:
        ops_registry.finish(task_id, status="failed", detail="Mem0Unavailable")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        ops_registry.finish(task_id, status="failed", detail="ValueError")
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        ops_registry.finish(task_id, status="failed", detail=type(exc).__name__)
        raise


@router.get("/benchmarks/workbench")
def get_memory_benchmark_workbench(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    case_index: int = Query(default=0, ge=0, le=9),
    session_index: int = Query(default=1, ge=1),
    question_index: int = Query(default=0, ge=0),
    preview_limit: int = Query(default=8, ge=1, le=20),
):
    try:
        payload = get_memory_workbench_service().load(
            case_index, session_index, question_index, preview_limit
        )
        payload["runtime_overview"] = ops_overview(db, user.tenant_id, limit=20)
        return payload
    except (IndexError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/benchmarks/recall")
def recall_memory_benchmark(body: MemoryBenchmarkRecallIn, user: CurrentUser):
    task_id = ops_registry.start(user.tenant_id, "memory.benchmark", "执行 LoCoMo 公开集 Mem0 评测")
    try:
        result = get_memory_workbench_service().recall(
            case_index=body.case_index,
            question_index=body.question_index,
            query=body.query,
            limit=body.limit,
            session_index=body.session_index,
            across_sessions=body.across_sessions,
        )
        ops_registry.finish(task_id, status="completed", detail=f"命中 {result['result_count']} 条")
        return result
    except Mem0Unavailable as exc:
        ops_registry.finish(task_id, status="failed", detail="Mem0Unavailable")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (IndexError, RuntimeError, ValueError) as exc:
        ops_registry.finish(task_id, status="failed", detail=type(exc).__name__)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        ops_registry.finish(task_id, status="failed", detail=type(exc).__name__)
        raise


@router.get("", response_model=list[MemoryOut])
def get_memories(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    search: str = Query(default="", max_length=120),
    memory_type: str = Query(default="", max_length=24),
    channel: str = Query(default="", max_length=30),
    contact_id: str = Query(default="", max_length=120),
    limit: int = Query(default=100, ge=1, le=200),
):
    memories = memory_crud.list_memories(
        db, user.tenant_id, search=search, memory_type=memory_type,
        channel=channel, contact_id=contact_id, limit=limit,
    )
    return [_out(memory) for memory in memories]


@router.get("/stats", response_model=MemoryStatsOut)
def get_memory_stats(user: CurrentUser, db: Annotated[Session, Depends(get_db)]):
    return MemoryStatsOut(**memory_crud.memory_stats(db, user.tenant_id))


@router.get("/{memory_id}/history")
def get_memory_history(
    memory_id: int, user: CurrentUser, db: Annotated[Session, Depends(get_db)],
):
    try:
        rows = get_customer_memory_service().memory_history(
            db, tenant_id=user.tenant_id, memory_id=memory_id
        )
    except Mem0Unavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if rows is None:
        raise HTTPException(status_code=404, detail="记忆不存在")
    return {"events": rows}


@router.get("/decisions/recent")
def get_recent_memory_decisions(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    channel: str = Query(default="", max_length=30),
    contact_id: str = Query(default="", max_length=120),
    limit: int = Query(default=100, ge=1, le=200),
):
    return {"events": get_customer_memory_service().list_semantic_decisions(
        db, tenant_id=user.tenant_id, channel=channel, contact_id=contact_id, limit=limit
    )}


@router.delete("/decisions")
def remove_memory_decisions(
    body: MemoryDecisionDeleteIn,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
):
    deleted = get_customer_memory_service().delete_semantic_decisions(
        db, tenant_id=user.tenant_id, decision_ids=body.ids
    )
    return {"deleted": deleted}


@router.post("", response_model=MemoryOut, status_code=status.HTTP_201_CREATED)
def add_memory(
    body: MemoryCreate, user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
):
    try:
        memory = get_customer_memory_service().create_manual_memory(
            db, tenant_id=user.tenant_id, channel=body.channel,
            contact_id=body.contact_id, memory_type=body.memory_type,
            content=body.content, importance=body.importance, is_pinned=body.is_pinned,
        )
        return _out(memory)
    except Mem0Unavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.patch("/{memory_id}", response_model=MemoryOut)
def edit_memory(
    memory_id: int, body: MemoryUpdate, user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
):
    try:
        memory = get_customer_memory_service().update_memory(
            db, tenant_id=user.tenant_id, memory_id=memory_id,
            **body.model_dump(exclude_unset=True),
        )
    except Mem0Unavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if memory is None:
        raise HTTPException(status_code=404, detail="记忆不存在")
    return _out(memory)


@router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_memory(
    memory_id: int, user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
):
    try:
        removed = get_customer_memory_service().delete_memory(
            db, tenant_id=user.tenant_id, memory_id=memory_id
        )
    except Mem0Unavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(status_code=404, detail="记忆不存在")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
