"""客户标签 CRUD —— 全部按 tenant_id 隔离（跨租户不可见/不可改/不可挂）。"""
from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.tag import CustomerTag, Tag


class TagNameExists(Exception):
    """同租户下标签名已存在。"""


# ---------- 标签本身 ----------

def create_tag(db: Session, tenant_id: int, name: str, *, color: str = "",
               description: str = "", group_name: str = "", ai_muted: bool = False) -> Tag:
    tag = Tag(tenant_id=tenant_id, name=name.strip(), color=color,
              description=description, group_name=group_name, ai_muted=ai_muted)
    db.add(tag)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise TagNameExists(name)
    db.refresh(tag)
    return tag


def list_tags(db: Session, tenant_id: int) -> list[Tag]:
    return list(db.scalars(
        select(Tag).where(Tag.tenant_id == tenant_id).order_by(Tag.group_name, Tag.name)
    ).all())


def get_tag(db: Session, tenant_id: int, tag_id: int) -> Tag | None:
    return db.scalar(
        select(Tag).where(Tag.id == tag_id, Tag.tenant_id == tenant_id)   # 租户闸
    )


def update_tag(db: Session, tenant_id: int, tag_id: int, **fields) -> Tag | None:
    tag = get_tag(db, tenant_id, tag_id)
    if tag is None:
        return None
    allowed = {"name", "color", "description", "group_name", "ai_muted"}
    for k, v in fields.items():
        if k in allowed and v is not None:
            setattr(tag, k, v.strip() if k == "name" and isinstance(v, str) else v)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise TagNameExists(fields.get("name", ""))
    db.refresh(tag)
    return tag


def delete_tag(db: Session, tenant_id: int, tag_id: int) -> bool:
    tag = get_tag(db, tenant_id, tag_id)
    if tag is None:
        return False
    # 手动级联：先删关联（无 DB 级 ondelete），再删标签。均带 tenant 闸。
    db.execute(delete(CustomerTag).where(
        CustomerTag.tenant_id == tenant_id, CustomerTag.tag_id == tag_id))
    db.delete(tag)
    db.commit()
    return True


# ---------- 客户↔标签 ----------

def attach_tag(db: Session, tenant_id: int, tag_id: int, channel: str,
               contact_id: str) -> CustomerTag | None:
    """给客户打标签。标签不存在/不属本租户 → None。已打过 → 幂等返回现有关联。"""
    if get_tag(db, tenant_id, tag_id) is None:
        return None
    existing = db.scalar(select(CustomerTag).where(
        CustomerTag.tenant_id == tenant_id, CustomerTag.tag_id == tag_id,
        CustomerTag.channel == channel, CustomerTag.contact_id == contact_id))
    if existing is not None:
        return existing
    link = CustomerTag(tenant_id=tenant_id, tag_id=tag_id, channel=channel, contact_id=contact_id)
    db.add(link)
    try:
        db.commit()
    except IntegrityError:                     # 并发下另一写入者已建同键
        db.rollback()
        return db.scalar(select(CustomerTag).where(
            CustomerTag.tenant_id == tenant_id, CustomerTag.tag_id == tag_id,
            CustomerTag.channel == channel, CustomerTag.contact_id == contact_id))
    db.refresh(link)
    return link


def detach_tag(db: Session, tenant_id: int, tag_id: int, channel: str, contact_id: str) -> bool:
    res = db.execute(delete(CustomerTag).where(
        CustomerTag.tenant_id == tenant_id, CustomerTag.tag_id == tag_id,
        CustomerTag.channel == channel, CustomerTag.contact_id == contact_id))
    db.commit()
    return res.rowcount > 0


def list_customer_tags(db: Session, tenant_id: int, channel: str, contact_id: str) -> list[Tag]:
    return list(db.scalars(
        select(Tag).join(CustomerTag, CustomerTag.tag_id == Tag.id).where(
            CustomerTag.tenant_id == tenant_id,
            CustomerTag.channel == channel,
            CustomerTag.contact_id == contact_id,
        ).order_by(Tag.group_name, Tag.name)
    ).all())


def customer_ai_muted(db: Session, tenant_id: int, channel: str, contact_id: str) -> bool:
    """该客户是否被任一标签设为 AI 静音（不自动回复、转人工）。"""
    return db.scalar(
        select(Tag.id).join(CustomerTag, CustomerTag.tag_id == Tag.id).where(
            CustomerTag.tenant_id == tenant_id,
            CustomerTag.channel == channel,
            CustomerTag.contact_id == contact_id,
            Tag.ai_muted.is_(True),
        ).limit(1)
    ) is not None


# ---------- 会话级 AI 托管开关（= WeiClaw setAiEnabled 心智：关=转人工/开=恢复AI）----------

AI_MUTE_TAG_NAME = "AI静音"      # 保留标签：控制台「AI 托管开关」关闭时给客户挂它


def _get_or_create_ai_mute_tag(db: Session, tenant_id: int) -> Tag:
    tag = db.scalar(select(Tag).where(Tag.tenant_id == tenant_id, Tag.name == AI_MUTE_TAG_NAME))
    if tag is not None:
        return tag
    try:
        return create_tag(db, tenant_id, AI_MUTE_TAG_NAME, color="#C84A3A",
                          description="AI 托管已关闭，该客户消息转人工处理", ai_muted=True)
    except TagNameExists:                              # 并发下已被建
        return db.scalar(select(Tag).where(Tag.tenant_id == tenant_id, Tag.name == AI_MUTE_TAG_NAME))


def set_customer_ai_mute(db: Session, tenant_id: int, channel: str, contact_id: str,
                         muted: bool) -> bool:
    """会话级开关：muted=True 给客户挂「AI静音」标签（转人工）；False 取下（恢复AI）。返回最终状态。"""
    tag = _get_or_create_ai_mute_tag(db, tenant_id)
    if muted:
        attach_tag(db, tenant_id, tag.id, channel, contact_id)
    else:
        detach_tag(db, tenant_id, tag.id, channel, contact_id)
    return muted
