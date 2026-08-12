"""WeComContactSource —— 让企微联系人出现在群发页（对齐 broadcast.contacts.ContactSource 协议）。

个人微信的 HookContactSource 读 contact.db 出好友；企微这边没有那套 hook，但 WeComHookAdapter
的 list_sessions()（经进程内自查读全量历史）已经能列出**所有有聊天记录的企微联系人**。
本源就把它映射成 broadcast 需要的 Friend 列表，从而群发页/聊天记录页都能看到全部企微联系人。
"""
from __future__ import annotations

from widget.broadcast.contacts import Friend


class WeComContactSource:
    """从 WeComHookAdapter.list_sessions() 出企微联系人（有聊天记录的会话对方）。"""

    def __init__(self, adapter):
        self._adapter = adapter          # 需有 list_sessions() -> list[{wxid,name,...}]

    def list_friends(self) -> list[Friend]:
        try:
            sessions = self._adapter.list_sessions()
        except Exception:
            return []
        out: list[Friend] = []
        seen = set()
        for s in sessions or []:
            wxid = str(s.get("wxid", "") or "")
            if not wxid or wxid in seen:
                continue
            # 只要 1:1 联系人（uid 形态），跳过群/系统会话（S:/R:/Y: 前缀的原始会话 id 不算联系人）
            if wxid[:2] in ("S:", "R:", "Y:", "B:"):
                continue
            seen.add(wxid)
            name = str(s.get("name", "") or wxid)
            out.append(Friend(wxid=wxid, nick=name, remark="", is_friend=True))
        return out
