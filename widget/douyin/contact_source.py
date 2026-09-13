from __future__ import annotations

from widget.broadcast.contacts import Friend


class DouyinContactSource:
    """Expose observed Douyin private-message users to broadcast/contact views."""

    def __init__(self, adapter) -> None:
        self._adapter = adapter

    def set_adapter(self, adapter) -> None:
        self._adapter = adapter

    def list_friends(self) -> list[Friend]:
        try:
            sessions = self._adapter.list_sessions()
        except Exception:
            return []
        return [
            Friend(
                wxid=str(item.get("wxid") or ""),
                nick=str(item.get("name") or item.get("wxid") or ""),
                remark="抖音私信用户",
                is_friend=True,
            )
            for item in sessions
            if item.get("wxid")
        ]
