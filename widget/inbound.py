from __future__ import annotations
from collections import deque
from widget.models import InboundMsg
from widget.wechat_accounts import is_official_or_system_account

_SYSTEM_SENDERS = {"", "weixin", "filehelper_system"}

class InboundFilter:
    def __init__(self, seen_max: int = 4000) -> None:
        self._seen: set[str] = set()          # 去重集合，有界（24/7 挂件不能无限增长）
        self._order: deque[str] = deque()     # 记录插入顺序，超上限淘汰最旧
        self._seen_max = seen_max
    def accept(self, msg: InboundMsg, self_wxid: str) -> bool:
        if not msg["text"] or not msg["text"].strip():
            return False
        sender = msg["sender_id"]
        if sender == self_wxid:                     # 自己发的
            return False
        if sender in _SYSTEM_SENDERS:               # 系统
            return False
        # 同时检查真实发送者和会话 ID：公众号可能表现为 gh_ 发送者，也可能只暴露
        # brandsessionholder / brandservicesessionholder 这类聚合会话占位账号。
        if is_official_or_system_account(sender) or \
                is_official_or_system_account(msg["contact_id"]):
            return False
        if msg["msg_id"] in self._seen:             # 去重
            return False
        self._seen.add(msg["msg_id"])               # 仅通过的消息才记入（拒绝的不占 msg_id）
        self._order.append(msg["msg_id"])
        if len(self._order) > self._seen_max:        # 有界：淘汰最旧
            self._seen.discard(self._order.popleft())
        return True
