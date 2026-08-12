from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from app.deps import CurrentUser
from app.llm import get_llm

router = APIRouter(prefix="/v1/broadcast", tags=["broadcast"])

_SYSTEM = (
    "你是电商私域运营的文案助手。根据商家的一句话需求，写一条面向微信好友的群发文案："
    "口语、亲切、简洁（60 字内），不含『转账/私聊/点击链接』等易触发风控的词，"
    "可用 {昵称} 作为收件人称呼占位。只输出文案本身。"
)


class DraftIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    brief: str
    tone: str | None = None


class DraftOut(BaseModel):
    draft: str


@router.post("/draft", response_model=DraftOut)
def draft(body: DraftIn, user: CurrentUser):
    tone = f"\n语气：{body.tone}" if body.tone else ""
    text = get_llm().chat(_SYSTEM, body.brief + tone)
    return DraftOut(draft=text)
