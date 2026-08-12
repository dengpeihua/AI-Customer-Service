from __future__ import annotations
from widget.config import ScopeConfig
from widget.models import InboundMsg


def conversation_key(channel: str, contact_id: str) -> str:
    """生成配置中稳定、可读的渠道级会话键。"""
    return f"{channel}|{contact_id}"

def should_handle(msg: InboundMsg, scope: ScopeConfig) -> bool:
    contact = msg["contact_id"]
    key = conversation_key(msg.get("channel", ""), contact)
    # 「接待此客户/会话」是所有渠道的第一道门。此前群聊分支在这两项检查之前直接返回，
    # 导致群聊上勾选/取消开关完全无效，并可能在 auto_send 开启后回复所有群。
    if key in scope.conversation_blocklist:
        return False
    if scope.private_mode == "selected" and key not in scope.conversation_allowlist:
        return False
    if msg["is_group"]:
        if not scope.allow_group:
            return False
        room = msg["contact_id"]
        if room in scope.group_blocklist:
            return False
        if scope.group_trigger == "all":
            return True
        if scope.group_trigger == "at_me":
            return msg["at_me"]
        if scope.group_trigger == "whitelist":
            return room in scope.group_whitelist
        return False
    # 私聊
    if not scope.allow_private:
        return False
    if contact in scope.contact_blocklist:
        return False
    if scope.contact_allowlist:                       # 白名单模式：仅名单内
        return contact in scope.contact_allowlist
    return True
