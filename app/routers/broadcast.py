from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from app.deps import CurrentUser
from app.llm import get_llm

router = APIRouter(prefix="/v1/broadcast", tags=["broadcast"])

_SYSTEM = (
    "你是抖音客户触达文案助手。根据商家的一句话需求，生成自然、可信、简洁的私信草稿；"
    "不虚构体验，不诱导私下交易，不承诺未经证实的效果。只输出文案本身。"
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
