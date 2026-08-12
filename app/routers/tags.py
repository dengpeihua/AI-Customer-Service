"""客户标签 API —— 全部按 token 里的 tenant_id 隔离。

标签本身：   POST/GET /v1/tags,  PATCH/DELETE /v1/tags/{id}
客户↔标签：  GET  /v1/customers/tags?channel=&contact_id=（列某客户的标签）
             POST /v1/customers/tags（打标签）
             DELETE /v1/customers/tags（取消标签）
"""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.crud import tag as tag_crud
from app.db import get_db
from app.deps import CurrentUser

router = APIRouter(prefix="/v1", tags=["tags"])


# ---------- schemas ----------

class TagIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    color: str = Field(default="", max_length=20)
    description: str = ""
    group_name: str = Field(default="", max_length=60)
    ai_muted: bool = False


class TagUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    color: str | None = Field(default=None, max_length=20)
    description: str | None = None
    group_name: str | None = Field(default=None, max_length=60)
    ai_muted: bool | None = None


class TagOut(BaseModel):
    id: int
    name: str
    color: str
    description: str
    group_name: str
    ai_muted: bool

    @classmethod
    def of(cls, t) -> "TagOut":
        return cls(id=t.id, name=t.name, color=t.color, description=t.description,
                   group_name=t.group_name, ai_muted=t.ai_muted)


class AttachIn(BaseModel):
    channel: str = Field(min_length=1, max_length=30)
    contact_id: str = Field(min_length=1, max_length=120)
    tag_id: int


# ---------- 标签本身 ----------

@router.post("/tags", response_model=TagOut, status_code=status.HTTP_201_CREATED)
def create_tag(body: TagIn, user: CurrentUser, db: Annotated[Session, Depends(get_db)]):
    try:
        tag = tag_crud.create_tag(db, user.tenant_id, body.name, color=body.color,
                                  description=body.description, group_name=body.group_name,
                                  ai_muted=body.ai_muted)
    except tag_crud.TagNameExists:
        raise HTTPException(status.HTTP_409_CONFLICT, "标签名已存在")
    return TagOut.of(tag)


@router.get("/tags", response_model=list[TagOut])
def list_tags(user: CurrentUser, db: Annotated[Session, Depends(get_db)]):
    return [TagOut.of(t) for t in tag_crud.list_tags(db, user.tenant_id)]


@router.patch("/tags/{tag_id}", response_model=TagOut)
def update_tag(tag_id: int, body: TagUpdate, user: CurrentUser,
               db: Annotated[Session, Depends(get_db)]):
    try:
        tag = tag_crud.update_tag(db, user.tenant_id, tag_id, **body.model_dump(exclude_unset=True))
    except tag_crud.TagNameExists:
        raise HTTPException(status.HTTP_409_CONFLICT, "标签名已存在")
    if tag is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "标签不存在")
    return TagOut.of(tag)


@router.delete("/tags/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_tag(tag_id: int, user: CurrentUser, db: Annotated[Session, Depends(get_db)]):
    if not tag_crud.delete_tag(db, user.tenant_id, tag_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "标签不存在")


# ---------- 客户↔标签 ----------

@router.get("/customers/tags", response_model=list[TagOut])
def list_customer_tags(channel: str, contact_id: str, user: CurrentUser,
                       db: Annotated[Session, Depends(get_db)]):
    return [TagOut.of(t) for t in
            tag_crud.list_customer_tags(db, user.tenant_id, channel, contact_id)]


@router.post("/customers/tags", status_code=status.HTTP_204_NO_CONTENT)
def attach_customer_tag(body: AttachIn, user: CurrentUser,
                        db: Annotated[Session, Depends(get_db)]):
    link = tag_crud.attach_tag(db, user.tenant_id, body.tag_id, body.channel, body.contact_id)
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "标签不存在")


@router.delete("/customers/tags", status_code=status.HTTP_204_NO_CONTENT)
def detach_customer_tag(channel: str, contact_id: str, tag_id: int, user: CurrentUser,
                        db: Annotated[Session, Depends(get_db)]):
    if not tag_crud.detach_tag(db, user.tenant_id, tag_id, channel, contact_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "该客户未打此标签")


# ---------- 会话级 AI 托管开关（转人工/恢复AI）----------

class AiMuteIn(BaseModel):
    channel: str = Field(min_length=1, max_length=30)
    contact_id: str = Field(min_length=1, max_length=120)
    muted: bool


class AiMuteOut(BaseModel):
    muted: bool


@router.get("/customers/ai-mute", response_model=AiMuteOut)
def get_ai_mute(channel: str, contact_id: str, user: CurrentUser,
                db: Annotated[Session, Depends(get_db)]):
    return AiMuteOut(muted=tag_crud.customer_ai_muted(db, user.tenant_id, channel, contact_id))


@router.post("/customers/ai-mute", response_model=AiMuteOut)
def set_ai_mute(body: AiMuteIn, user: CurrentUser, db: Annotated[Session, Depends(get_db)]):
    tag_crud.set_customer_ai_mute(db, user.tenant_id, body.channel, body.contact_id, body.muted)
    return AiMuteOut(muted=body.muted)
