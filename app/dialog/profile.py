from __future__ import annotations

from sqlalchemy.orm import Session

from app.config import settings
from app.llm.base import LLM
from app.memory.customer_service import get_customer_memory_service


_STOPWORDS = {"好的", "好", "谢谢", "谢啦", "嗯", "嗯嗯", "收到", "ok", "okay", "在吗", "你好", "在"}


def update_customer_profile(
    db: Session,
    llm: LLM,
    *,
    tenant_id: int,
    channel: str,
    contact_id: str,
    customer_text: str,
    reply_text: str,
    exchange_id: str = "",
) -> None:
    """Use Mem0 to consolidate one live exchange into durable customer memory.

    The caller already runs this in a guarded background task. Short greetings are still filtered
    locally to avoid unnecessary model cost; Mem0 owns fact extraction and ADD/UPDATE/DELETE
    consolidation, while ``CustomerProfile`` is rebuilt as a deterministic view of its memories.
    """
    _ = llm  # Kept in the public signature for existing chat/wecom call sites.
    text = (customer_text or "").strip()
    if len(text) < settings.profile_min_chars or text.lower() in _STOPWORDS:
        return
    get_customer_memory_service().remember_exchange(
        db, tenant_id=tenant_id, channel=channel, contact_id=contact_id,
        customer_text=text, reply_text=reply_text or "", exchange_id=exchange_id,
    )
