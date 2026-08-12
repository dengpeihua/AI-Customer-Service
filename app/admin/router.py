import math
from pathlib import Path
from typing import Annotated
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.config import settings
from app.db import get_db
from app.crud.user import bump_session_version, create_user, get_user_by_login
from app.security import verify_password
from app.admin.deps import AdminUser, admin_user, current_admin_user, require_admin
from app.dialog.tone import TONE_PRESETS
from app.admin.ratelimit import account_limiter, ip_limiter
from app.admin.session import make_session
from app.admin import metrics
from app.crud import bot as bot_crud
from app.crud import deal as deal_crud
from app.crud import kb as kb_crud
from app.crud import tag as tag_crud
from app.kb.ingest import ingest_document
from app.kb.upload import KbUploadError, ingest_upload
from app.llm import get_llm
from app.models.conversation import Conversation, Message
from app.dialog.delivery import message_was_delivered
from app.models.user import User

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))

CONVS_PER_PAGE = 20


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"request": request, "error": None})


@router.post("/login")
def login_submit(request: Request, db: Annotated[Session, Depends(get_db)],
                 tenant_id: str = Form("1"), login: str = Form(""), password: str = Form("")):
    # 交付健壮化：单租户产品里客户通常不知道「租户ID」填什么，留空/带格式（手机号 138 0013…）
    # 曾让 FastAPI 抛原始 422 JSON（"字符串类型错误"），客户被卡死无从下手。这里一律接成
    # 字符串自行校验，任何非法输入都回登录页给中文友好提示，绝不甩原始 422 给客户。
    def _err(msg: str, code: int = 422):
        return templates.TemplateResponse(request, "login.html",
            {"request": request, "error": msg}, status_code=code)

    raw = (tenant_id or "").strip()
    try:
        tid = int(raw) if raw else 1          # 留空按默认租户 1（单租户交付；模板已预填 1）
    except ValueError:
        return _err("租户ID 请填数字（一般填 1）")
    login = (login or "").strip()
    if not login or not password:
        return _err("请填写登录名和密码")

    acct_key = f"{tid}:{login}"
    ip_key = request.client.host if request.client else "unknown"

    # 锁定期内先拦，密码对不对都不看——否则爆破者最后一次猜中就绕过了
    if account_limiter.is_locked(acct_key) or ip_limiter.is_locked(ip_key):
        return templates.TemplateResponse(request, "login.html",
            {"request": request, "error": "登录失败次数过多，请稍后再试"}, status_code=429)

    user = get_user_by_login(db, tid, login)
    if user is None or not verify_password(password, user.pwd_hash):
        account_limiter.record_failure(acct_key)
        ip_limiter.record_failure(ip_key)
        return templates.TemplateResponse(request, "login.html",
            {"request": request, "error": "登录名或密码错误"}, status_code=401)

    account_limiter.reset(acct_key)
    ip_limiter.reset(ip_key)
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie("admin_session", make_session(user), httponly=True, samesite="strict",
                    secure=settings.admin_cookie_secure, max_age=12 * 3600)
    return resp


@router.post("/logout")
def logout(request: Request, db: Annotated[Session, Depends(get_db)]):
    user = current_admin_user(request, db)
    if user is not None:
        bump_session_version(db, user)  # 真正作废，被偷走的 cookie 也一起失效
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie("admin_session")
    return resp


@router.get("", response_class=HTMLResponse)
def dashboard(request: Request, user: Annotated[AdminUser, Depends(admin_user)],
              db: Annotated[Session, Depends(get_db)], range: str = "7d"):
    import datetime as _dt
    today = _dt.datetime.now(_dt.timezone.utc).date()
    span = {"today": 0, "7d": 6, "30d": 29}.get(range, 6)
    start = today - _dt.timedelta(days=span)
    m = metrics.dashboard_metrics(db, user.tenant_id, start, today)
    return templates.TemplateResponse(request, "dashboard.html", {
        "user": user, "active": "home", "m": m, "range": range,
        "trend": metrics.trend_ext(db, user.tenant_id, 7)})


@router.get("/kb", response_class=HTMLResponse)
def kb_page(request: Request, user: Annotated[AdminUser, Depends(admin_user)],
            db: Annotated[Session, Depends(get_db)]):
    docs = kb_crud.list_documents(db, user.tenant_id)
    return templates.TemplateResponse(request, "kb.html",
        {"user": user, "active": "kb", "docs": docs, "error": None})


@router.post("/kb")
def kb_add(user: Annotated[AdminUser, Depends(admin_user)], db: Annotated[Session, Depends(get_db)],
           title: str = Form(...), content: str = Form(...)):
    ingest_document(db, get_llm(), tenant_id=user.tenant_id, title=title, source_type="faq", content=content)
    return RedirectResponse("/admin/kb", status_code=303)


@router.post("/kb/upload")
async def kb_upload(request: Request, user: Annotated[AdminUser, Depends(admin_user)],
                    db: Annotated[Session, Depends(get_db)],
                    file: UploadFile = File(...), title: str = Form("")):
    raw = await file.read(settings.kb_upload_max_bytes + 1)
    try:
        ingest_upload(db, get_llm(), tenant_id=user.tenant_id,
                      filename=file.filename or "", raw=raw, title=title or None)
    except KbUploadError as exc:
        return templates.TemplateResponse(request, "kb.html", {
            "user": user, "active": "kb", "error": exc.detail,
            "docs": kb_crud.list_documents(db, user.tenant_id)}, status_code=exc.status_code)
    return RedirectResponse("/admin/kb", status_code=303)


@router.post("/kb/{doc_id}/delete")
def kb_delete(doc_id: int, user: Annotated[AdminUser, Depends(admin_user)],
              db: Annotated[Session, Depends(get_db)]):
    kb_crud.delete_document(db, user.tenant_id, doc_id)
    return RedirectResponse("/admin/kb", status_code=303)


@router.get("/bot", response_class=HTMLResponse)
def bot_page(request: Request, user: Annotated[AdminUser, Depends(admin_user)],
             db: Annotated[Session, Depends(get_db)]):
    cfg = bot_crud.get_or_create(db, user.tenant_id)
    return templates.TemplateResponse(request, "bot.html", {
        "user": user, "active": "bot", "cfg": cfg})


@router.post("/bot")
def bot_save(user: Annotated[AdminUser, Depends(admin_user)], db: Annotated[Session, Depends(get_db)],
             welcome: str = Form(""), persona: str = Form(""),
             tone_level: str = Form("warm")):
    tone = tone_level if tone_level in TONE_PRESETS else "warm"
    bot_crud.update(db, user.tenant_id, welcome=welcome, persona=persona,
                    tone_level=tone)
    return RedirectResponse("/admin/bot", status_code=303)


@router.get("/tags", response_class=HTMLResponse)
def tags_page(request: Request, user: Annotated[AdminUser, Depends(admin_user)],
              db: Annotated[Session, Depends(get_db)], error: str | None = None):
    return templates.TemplateResponse(request, "tags.html", {
        "user": user, "active": "tags", "tags": tag_crud.list_tags(db, user.tenant_id),
        "error": error})


@router.post("/tags")
def tags_add(request: Request, user: Annotated[AdminUser, Depends(admin_user)],
             db: Annotated[Session, Depends(get_db)],
             name: str = Form(...), color: str = Form(""), description: str = Form(""),
             group_name: str = Form(""), ai_muted: str = Form("")):
    if name.strip():
        try:
            tag_crud.create_tag(db, user.tenant_id, name, color=color, description=description,
                                group_name=group_name, ai_muted=bool(ai_muted))
        except tag_crud.TagNameExists:
            return templates.TemplateResponse(request, "tags.html", {
                "user": user, "active": "tags", "tags": tag_crud.list_tags(db, user.tenant_id),
                "error": f"标签「{name}」已存在"}, status_code=409)
    return RedirectResponse("/admin/tags", status_code=303)


@router.post("/tags/{tag_id}/delete")
def tags_delete(tag_id: int, user: Annotated[AdminUser, Depends(admin_user)],
                db: Annotated[Session, Depends(get_db)]):
    tag_crud.delete_tag(db, user.tenant_id, tag_id)
    return RedirectResponse("/admin/tags", status_code=303)


@router.post("/tags/{tag_id}/mute")
def tags_toggle_mute(tag_id: int, user: Annotated[AdminUser, Depends(admin_user)],
                     db: Annotated[Session, Depends(get_db)]):
    tag = tag_crud.get_tag(db, user.tenant_id, tag_id)
    if tag is not None:
        tag_crud.update_tag(db, user.tenant_id, tag_id, ai_muted=not tag.ai_muted)
    return RedirectResponse("/admin/tags", status_code=303)


@router.get("/conversations", response_class=HTMLResponse)
def conv_list(request: Request, user: Annotated[AdminUser, Depends(admin_user)],
              db: Annotated[Session, Depends(get_db)], page: int = 1):
    mine = Conversation.tenant_id == user.tenant_id
    total = db.scalar(select(func.count()).select_from(Conversation).where(mine)) or 0
    pages = max(1, math.ceil(total / CONVS_PER_PAGE))
    page = min(max(1, page), pages)  # 越界夹到首/末页，而不是给一张空白页
    convs = db.scalars(select(Conversation).where(mine).order_by(Conversation.id.desc())
                       .limit(CONVS_PER_PAGE).offset((page - 1) * CONVS_PER_PAGE)).all()
    return templates.TemplateResponse(request, "conversations.html",
        {"user": user, "active": "conversations", "convs": convs,
         "page": page, "pages": pages, "total": total})


@router.get("/conversations/{conv_id}", response_class=HTMLResponse)
def conv_detail(conv_id: int, request: Request, user: Annotated[AdminUser, Depends(admin_user)],
                db: Annotated[Session, Depends(get_db)]):
    conv = db.get(Conversation, conv_id)
    if conv is None or conv.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="not found")
    msgs = [
        message for message in db.scalars(
            select(Message).where(Message.conversation_id == conv.id).order_by(Message.id)
        ).all()
        if message_was_delivered(message)
    ]
    deal = deal_crud.get_deal_for_conversation(db, user.tenant_id, conv.id)
    return templates.TemplateResponse(request, "conversation_detail.html",
        {"user": user, "active": "conversations", "conv": conv, "msgs": msgs, "deal": deal})


@router.post("/conversations/{conv_id}/deal")
def conv_deal(conv_id: int, user: Annotated[AdminUser, Depends(admin_user)],
              db: Annotated[Session, Depends(get_db)],
              amount_yuan: float = Form(0.0), is_followup: str = Form(""), note: str = Form("")):
    try:
        deal_crud.record_deal(db, user.tenant_id, conv_id,
                              amount_cents=int(round(max(0.0, amount_yuan) * 100)),
                              is_followup=(is_followup == "on"), note=note)
    except ValueError:
        pass  # 跨租户/不存在：静默，不泄露
    return RedirectResponse(f"/admin/conversations/{conv_id}", status_code=303)


@router.get("/users", response_class=HTMLResponse)
def users_page(request: Request, user: Annotated[AdminUser, Depends(require_admin)],
               db: Annotated[Session, Depends(get_db)]):
    users = db.scalars(select(User).where(User.tenant_id == user.tenant_id).order_by(User.id)).all()
    return templates.TemplateResponse(request, "users.html",
        {"user": user, "active": "users", "users": users})


@router.post("/users")
def users_add(user: Annotated[AdminUser, Depends(require_admin)], db: Annotated[Session, Depends(get_db)],
              login: str = Form(...), password: str = Form(...), role: str = Form("agent")):
    create_user(db, tenant_id=user.tenant_id, login=login, password=password, role=role)
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{uid}/delete")
def users_delete(uid: int, user: Annotated[AdminUser, Depends(require_admin)],
                  db: Annotated[Session, Depends(get_db)]):
    if uid == user.id:  # 禁止删自己
        return RedirectResponse("/admin/users", status_code=303)
    u = db.get(User, uid)
    if u is not None and u.tenant_id == user.tenant_id:
        db.delete(u)
        db.commit()
    return RedirectResponse("/admin/users", status_code=303)
