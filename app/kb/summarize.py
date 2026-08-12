from sqlalchemy import select
from sqlalchemy.orm import Session

from app.dialog.tone import TONE_HANDOFFS
from app.dialog.delivery import message_was_delivered
from app.kb.ingest import ingest_document
from app.llm.base import LLM
from app.models.conversation import Conversation, Message
from app.models.knowledge import KbDocument

_SYSTEM = (
    "你是客服知识库整理助手。把下面这段客服对话提炼成一条可复用的 FAQ："
    "第一行是客户的核心问题，接着给出标准答法。简洁、去掉寒暄与客户个人信息。"
)

# 企微本地库历史反哺用：收割出的是**无角色标注、客户与客服混在一起**的一批消息片段，
# 不是结构化对话，故用另一套提示词让 LLM 从中挑出业务问答、丢噪声。
_HISTORY_SYSTEM = (
    "你是客服知识库整理助手。下面是从客服聊天记录里收割的一批消息片段（客户与客服混在一起、无角色标注、可能有系统提示）。"
    "请从中提炼出可复用的 FAQ：每条一行客户常见问题，紧跟一行标准答法；"
    "只保留与业务/产品/价格/售后相关的问答，丢掉寒暄、纯客户个人信息、系统提示与无意义片段。"
    "如果没有任何可提炼的业务问答，就只回四个字：无可提炼。"
)
_NOTHING = "无可提炼"


class KbSummarizeError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def summarize_conversation_to_kb(db: Session, llm: LLM, *, tenant_id: int, channel: str,
                                 contact_id: str) -> KbDocument:
    conv = db.scalar(
        select(Conversation).where(
            Conversation.tenant_id == tenant_id,
            Conversation.channel == channel,
            Conversation.contact_id == contact_id,
        ).order_by(Conversation.id.desc()).limit(1)
    )
    if conv is None:
        raise KbSummarizeError(400, "该客户还没有可总结的对话")

    # 幂等：同一会话已总结过 → 直接返回旧 FAQ，不重复入库（挡双击/客户端超时重试）
    existing = db.scalar(
        select(KbDocument).where(
            KbDocument.tenant_id == tenant_id,
            KbDocument.source_conversation_id == conv.id,
        ).limit(1)
    )
    if existing is not None:
        return existing

    msgs = [
        message for message in db.scalars(
        select(Message).where(Message.conversation_id == conv.id).order_by(Message.id)
        ).all()
        if message_was_delivered(message)
    ]
    if len(msgs) < 2 or not any(m.sender == "customer" for m in msgs):
        raise KbSummarizeError(400, "该客户还没有可总结的对话")
    # 守卫：至少要有一条「非转人工套话」的客服回答，否则会蒸出以转人工话术当答案的
    # 劣质 FAQ 进共享库，之后还可能被检索到自动回给别的客户。
    if not any(m.sender in ("ai", "agent") and (m.text or "").strip() not in TONE_HANDOFFS
               for m in msgs):
        raise KbSummarizeError(400, "这段对话还没有有效解答（都转了人工），暂不适合入库")

    transcript = "\n".join(
        f"{'客户' if m.sender == 'customer' else '客服'}：{m.text}" for m in msgs
    )
    faq = llm.chat(_SYSTEM, transcript).strip()
    if not faq:
        raise KbSummarizeError(400, "总结失败，请稍后再试")
    title = faq.splitlines()[0].strip()[:300]
    doc = ingest_document(db, llm, tenant_id=tenant_id, title=title,
                          source_type="faq", content=faq)
    doc.source_conversation_id = conv.id       # 标记来源会话，供下次去重
    db.commit()
    return doc


def summarize_texts_to_kb(db: Session, llm: LLM, *, tenant_id: int,
                          texts: list[str], title: str = "企微本地历史反哺") -> KbDocument:
    """把企微本地库收割出的一批历史消息文本提炼成 FAQ 反哺知识库。

    与 summarize_conversation_to_kb 的区别：输入是**无角色、去重后的裸文本列表**（来自
    col_hook 被动收割的 message.db 解密明文，含连接前历史 + 人工手打），不是结构化 Conversation。
    不做 source_conversation_id 去重（无会话 id）；MsgDbReader 增量游标已保证重复点击只喂新增。
    """
    seen: set[str] = set()
    clean: list[str] = []
    for t in texts:
        s = (t or "").strip()
        if s and s not in seen:
            seen.add(s)
            clean.append(s)
    if len(clean) < 2:
        raise KbSummarizeError(400, "本地库暂无足够可反哺的历史")
    faq = llm.chat(_HISTORY_SYSTEM, "\n".join(clean)).strip()
    if not faq or faq.replace("。", "").replace(" ", "") == _NOTHING:
        raise KbSummarizeError(400, "这批历史里没有可提炼成 FAQ 的业务问答")
    doc_title = (title or faq.splitlines()[0].strip())[:300]
    return ingest_document(db, llm, tenant_id=tenant_id, title=doc_title,
                           source_type="history", content=faq)
