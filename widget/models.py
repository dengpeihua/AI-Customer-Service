from __future__ import annotations
from dataclasses import dataclass
from typing import NotRequired, TypedDict

class InboundMsg(TypedDict):
    channel: str  # 每实例 channel_key，如 douyin#account_a
    msg_id: str
    contact_id: str      # 私聊=对方 wxid；群=群 id（回复目标）
    sender_id: str       # 群里的实际发言人 wxid（私聊=contact_id）
    text: str
    is_group: bool
    at_me: bool
    timestamp: int
    sender_name: NotRequired[str]
    sender_avatar: NotRequired[str]
    kind: NotRequired[str]
    media_url: NotRequired[str]
    title: NotRequired[str]
    description: NotRequired[str]
    url: NotRequired[str]
    display_time: NotRequired[str]
    video_url: NotRequired[str]
    media_path: NotRequired[str]
    thumb_path: NotRequired[str]
    duration_ms: NotRequired[int]
    quote_sender: NotRequired[str]
    quote_text: NotRequired[str]
    source_type: NotRequired[str]
    feed_id: NotRequired[str]
    delivery_replay: NotRequired[bool]

@dataclass
class SendResult:
    ok: bool
    error: str = ""
    uncertain: bool = False
