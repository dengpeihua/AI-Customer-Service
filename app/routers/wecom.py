import logging
from typing import Annotated
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session
from app.db import SessionLocal, get_db
from app.deps import CurrentUser
from app.llm import get_llm
from app.crud import wecom as wecom_crud
from app.channels.wecom import service
from app.channels.wecom.client import WeComClient
from app.schemas.wecom import WeComConfigIn, WeComConfigOut

router = APIRouter(prefix="/v1/channels/wecom", tags=["wecom"])
_log = logging.getLogger(__name__)
_client = WeComClient()          # 进程内单例，含 access_token 缓存
session_factory = SessionLocal   # 后台任务自开会话用；测试会覆盖成测试库的工厂


@router.post("/config")
def set_wecom_config(body: WeComConfigIn, user: CurrentUser, db: Annotated[Session, Depends(get_db)]):
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin only")
    wecom_crud.set_config(db, user.tenant_id, corp_id=body.corp_id, secret=body.secret,
                          callback_token=body.callback_token,
                          encoding_aes_key=body.encoding_aes_key, open_kfid=body.open_kfid)
    return {"ok": True}


@router.get("/config", response_model=WeComConfigOut)
def get_wecom_config(user: CurrentUser, db: Annotated[Session, Depends(get_db)]):
    cfg = wecom_crud.get_config(db, user.tenant_id)
    if cfg is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not configured")
    return WeComConfigOut.from_cfg(cfg)


@router.get("/callback/{tenant_id}", response_class=PlainTextResponse)
def wecom_verify(tenant_id: int, msg_signature: str, timestamp: str, nonce: str, echostr: str,
                 db: Annotated[Session, Depends(get_db)]):
    cfg = wecom_crud.get_config(db, tenant_id)
    if cfg is None or not cfg.enabled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad request")
    try:
        return service.handle_callback_get(cfg, msg_signature, timestamp, nonce, echostr)
    except service.WeComSecurityError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad request")


def _process_in_background(tenant_id: int, open_kfid: str, sync_token: str) -> None:
    """后台跑重活，自开会话（请求的 session 在响应返回时就关了，不能带进后台）。"""
    db = session_factory()
    try:
        cfg = wecom_crud.get_config(db, tenant_id)
        if cfg is None or not cfg.enabled:
            return
        service.process_kf_event(db, get_llm(), cfg, _client, open_kfid, sync_token)
        db.commit()
    except Exception as e:                  # 后台异常只记日志，企微那边早已收到 200
        db.rollback()
        _log.error("wecom background processing error: %s", type(e).__name__)
    finally:
        db.close()


@router.post("/callback/{tenant_id}")
async def wecom_callback(tenant_id: int, msg_signature: str, timestamp: str, nonce: str,
                         request: Request, background: BackgroundTasks,
                         db: Annotated[Session, Depends(get_db)]):
    """企业微信规定：回调 **5 秒内**不响应就断开并重试 3 次。拉消息 + 大模型 + 发送
    通常要 2–5 秒，同步做必然超时（企微会判定回调失败，失败多了可能停掉回调）。
    所以这里只在请求内做验签解密（坏签名照样 400），重活交后台，立刻返回 200。"""
    cfg = wecom_crud.get_config(db, tenant_id)
    if cfg is None or not cfg.enabled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad request")
    raw = (await request.body()).decode("utf-8")
    try:
        parsed = service.parse_callback_event(cfg, msg_signature, timestamp, nonce, raw)
    except service.WeComSecurityError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad request")
    except Exception as e:                  # 解析异常不外泄、不引发企微重试风暴
        _log.error("wecom callback parse error: %s", type(e).__name__)
        return Response(status_code=200)
    if parsed is not None:
        background.add_task(_process_in_background, tenant_id, parsed[0], parsed[1])
    return Response(status_code=200)
