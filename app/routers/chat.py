import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.db import SessionLocal, get_db
from app.deps import CurrentUser
from app.dialog.engine import answer
from app.dialog.profile import update_customer_profile
from app.llm import get_llm

router = APIRouter(prefix="/v1/chat", tags=["chat"])
_log = logging.getLogger(__name__)


class ChatIn(BaseModel):
    model_config = ConfigDict(extra="forbid")   # 未知字段(如企图注入 tenant_id)直接拒，fail-closed
    channel: str
    contact_id: str
    text: str
    conversation_id: int | None = None
    source_message_id: str | None = Field(default=None, max_length=200)


class KbHit(BaseModel):
    text: str
    distance: float


class ChatOut(BaseModel):
    conversation_id: int
    outbound_message_id: int
    reply_text: str
    action: str
    intent: str
    route_reason: str
    kb_hits: list[KbHit]
    deduplicated: bool = False
    delivery_status: str = ""


def run_profile_update(tenant_id: int, channel: str, contact_id: str,
                       customer_text: str, reply_text: str, exchange_id: str = "") -> None:
    """后台任务：自开 DB session + LLM 更新画像。全程兜异常，绝不影响已返回的回复。"""
    db = SessionLocal()
    try:
        update_customer_profile(db, get_llm(), tenant_id=tenant_id, channel=channel,
                                contact_id=contact_id, customer_text=customer_text,
                                reply_text=reply_text, exchange_id=exchange_id)
    except Exception:
        _log.warning("profile update failed", exc_info=True)
    finally:
        db.close()


@router.post("", response_model=ChatOut)
def chat(body: ChatIn, user: CurrentUser, db: Annotated[Session, Depends(get_db)],
         background_tasks: BackgroundTasks):
    result = answer(
        db, get_llm(), tenant_id=user.tenant_id, channel=body.channel,
        contact_id=body.contact_id, text=body.text, conversation_id=body.conversation_id,
        source_message_id=body.source_message_id,
    )
    if not result.get("deduplicated"):
        background_tasks.add_task(run_profile_update, user.tenant_id, body.channel,
                                 body.contact_id, body.text, result["reply_text"],
                                 result.get("memory_exchange_id", ""))
    return ChatOut(**result)
