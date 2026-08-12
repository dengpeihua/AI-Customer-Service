"""企微「微信客服」回调编排 + 全部安全校验。纯函数式，便于单测。"""
from __future__ import annotations
import logging
import time
from collections import deque
import defusedxml.ElementTree as ET
from app.config import settings
from app.channels.wecom import crypto
from app.crud.wecom import update_cursor
from app.dialog.engine import answer
from app.dialog.profile import update_customer_profile

_nonce_seen: deque = deque(maxlen=settings.wecom_nonce_cache)


class WeComSecurityError(Exception):
    pass


def reset_security_state() -> None:
    _nonce_seen.clear()


def _check_replay(tenant_id, timestamp: str, nonce: str) -> None:
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        raise WeComSecurityError("bad timestamp")
    if abs(time.time() - ts) > settings.wecom_replay_window_s:
        raise WeComSecurityError("stale timestamp")
    key = f"{tenant_id}:{nonce}"                    # 按租户隔离，防跨租户 nonce 碰撞误拒合法消息
    if key in _nonce_seen:
        raise WeComSecurityError("replayed nonce")
    _nonce_seen.append(key)                         # maxlen 自动淘汰最旧


def _extract_encrypt(raw_body: str) -> str:
    root = ET.fromstring(raw_body)                  # defusedxml：防 XXE
    node = root.find("Encrypt")
    if node is None or not node.text:
        raise WeComSecurityError("no Encrypt")
    return node.text


def _verify_and_decrypt(cfg, msg_signature, timestamp, nonce, encrypt) -> tuple[str, str]:
    if not crypto.verify_signature(cfg.callback_token, timestamp, nonce, encrypt, msg_signature):
        raise WeComSecurityError("bad signature")
    _check_replay(cfg.tenant_id, timestamp, nonce)
    msg, receiveid = crypto.decrypt(cfg.encoding_aes_key, encrypt)
    if receiveid != cfg.corp_id:                    # 租户绑定
        raise WeComSecurityError("receiveid mismatch")
    return msg, receiveid


def handle_callback_get(cfg, msg_signature, timestamp, nonce, echostr) -> str:
    if not crypto.verify_signature(cfg.callback_token, timestamp, nonce, echostr, msg_signature):
        raise WeComSecurityError("bad signature")
    plaintext, receiveid = crypto.decrypt(cfg.encoding_aes_key, echostr)
    if receiveid != cfg.corp_id:
        raise WeComSecurityError("receiveid mismatch")
    return plaintext


def parse_callback_event(cfg, msg_signature, timestamp, nonce, raw_body) -> tuple[str, str] | None:
    """请求内做的那一半：验签 + 解密 + 防重放 + 认出是不是客服消息事件。
    返回 (open_kfid, sync_token)；不是客服消息事件返回 None。
    刻意与 `process_kf_event` 分开——企微规定回调 **5 秒不响应就重试 3 次**，
    而拉消息+大模型+发送通常要 2–5 秒，只能把重活甩到后台，让路由立刻返回 200。"""
    encrypt = _extract_encrypt(raw_body)
    event_xml, _ = _verify_and_decrypt(cfg, msg_signature, timestamp, nonce, encrypt)
    root = ET.fromstring(event_xml)
    if (root.findtext("Event") or "") != "kf_msg_or_event":
        return None                                 # 非客服消息事件，忽略
    return (root.findtext("OpenKfId") or cfg.open_kfid, root.findtext("Token") or "")


def process_kf_event(db, llm, cfg, client, open_kfid: str, sync_token: str) -> None:
    """后台做的那一半：拉消息 → 交给 AI → 回复 → 推进游标。耗时，不能放在请求里。"""
    access_token = client.get_access_token(cfg.corp_id, cfg.secret)
    resp = client.sync_msg(access_token, sync_token, open_kfid, cfg.sync_cursor)
    for m in resp.get("msg_list", []):
        if m.get("msgtype") != "text":
            continue                                # 本期只处理 text
        external = m.get("external_userid", "")
        content = (m.get("text") or {}).get("content", "")
        if not external or not content.strip():
            continue
        try:
            result = answer(db, llm, tenant_id=cfg.tenant_id, channel="wecom",
                            contact_id=external, text=content, conversation_id=None)
            if (result.get("action") in {"auto_reply", "handoff"}
                    and result.get("reply_text")):
                client.send_text(access_token, open_kfid, external, result["reply_text"])
            try:
                update_customer_profile(db, llm, tenant_id=cfg.tenant_id, channel="wecom",
                                        contact_id=external, customer_text=content,
                                        reply_text=result.get("reply_text", ""),
                                        exchange_id=f"wecom:{m.get('msgid') or m.get('send_time') or ''}")
            except Exception:
                db.rollback()
        except Exception as e:
            logging.getLogger(__name__).error("wecom msg process error: %s", type(e).__name__)
            continue                            # 单条失败不中断整批，游标照常推进
    nxt = resp.get("next_cursor")
    if nxt:
        update_cursor(db, cfg.tenant_id, nxt)       # 持久化游标


def handle_callback_post(db, llm, cfg, client, msg_signature, timestamp, nonce, raw_body) -> None:
    """同步版：解析 + 处理一把梭。保留给测试与非 HTTP 场景（如轮询模式）直接调用；
    HTTP 回调路由走 parse_callback_event + 后台 process_kf_event，避免 5 秒超时。"""
    parsed = parse_callback_event(cfg, msg_signature, timestamp, nonce, raw_body)
    if parsed is None:
        return
    process_kf_event(db, llm, cfg, client, parsed[0], parsed[1])
