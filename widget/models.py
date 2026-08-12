from __future__ import annotations
from dataclasses import dataclass
from typing import TypedDict

class InboundMsg(TypedDict):
    channel: str  # 每实例 channel_key，如 wechat#boss/wecom#shopA；legacy 行用旧平台常数
    msg_id: str
    contact_id: str      # 私聊=对方 wxid；群=群 id（回复目标）
    sender_id: str       # 群里的实际发言人 wxid（私聊=contact_id）
    text: str
    is_group: bool
    at_me: bool
    timestamp: int

@dataclass
class SendResult:
    ok: bool
    error: str = ""
