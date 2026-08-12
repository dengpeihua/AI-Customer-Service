"""M3 转人工控制器：把「待人工」队列变成可操作的动作（多渠道）。

人工在转人工浮窗里对某条待办：
- reply_pending(pending_id, text)：按待办绑定的 channel 发出带原消息引用的回复；成功后只移除
  这个 pending_id，其他同客户消息继续保留。
- dismiss_pending(pending_id)：不回复，只标记这一条处理完。
- send(contact, text, channel)：普通人工发送，不隐式清空任何待办。

双渠道：个人微信的待办走个人微信桥、企微的待办走企微桥，绝不串台。channel 缺省=hub 默认渠道
（单渠道旧行为）。与 GUI 解耦，便于单测；浮窗只调这几个方法。
"""
from __future__ import annotations

from widget.reply_quote import send_reply


class HandoffController:
    def __init__(self, hub, state, on_changed=None):
        self.hub = hub                  # ChannelHub：按 channel 路由 adapter/pipeline
        self.state = state              # RuntimeState（pending / 计数）
        self._on_changed = on_changed or (lambda: None)

    def set_on_changed(self, callback) -> None:
        self._on_changed = callback or (lambda: None)

    def pending(self) -> list[dict]:
        snapshot = getattr(self.state, "pending_snapshot", None)
        return snapshot() if callable(snapshot) else list(self.state.pending)

    def _send(self, contact_id: str, text: str, channel: str | None,
              reply_to: dict | None) -> bool:
        if not text or not text.strip():
            return False
        adapter = self.hub.adapter(channel)
        if adapter is None:
            return False
        res = send_reply(adapter, contact_id, text, reply_to, provenance="human")
        if not res.ok:
            return False
        self.state.record_agent_reply(contact_id, text)
        if reply_to:
            self.state.publish_outbound(reply_to, text, "agent", quote=True)
        return True

    def send(self, contact_id: str, text: str, channel: str | None = None) -> bool:
        """发送普通人工消息；公开入口没有引用参数，确保非待办消息永不引用。"""
        return self._send(contact_id, text, channel, reply_to=None)

    def reply_pending(self, pending_id: str, text: str) -> bool:
        item = self.state.find_pending(pending_id)
        if item is None:
            return False
        contact_id = str(item.get("contact") or "")
        channel = str(item.get("channel") or "")
        if not self._send(contact_id, text, channel, reply_to=item):
            return False                # 发送失败：只保留这一条待办，人工可稍后重试
        pipe = self.hub.pipeline(channel)
        if pipe is not None:
            pipe.release_contact(contact_id)
        self.state.remove_pending(pending_id)
        self._on_changed()
        return True

    def reply(self, contact_id: str, text: str, channel: str | None = None) -> bool:
        """Compatibility path: complete only the oldest matching pending item."""
        item = next((pending for pending in self.pending()
                     if pending.get("contact") == contact_id
                     and (channel is None or pending.get("channel", "") == channel)), None)
        if item is None:
            return self.send(contact_id, text, channel)
        return self.reply_pending(str(item.get("id") or ""), text)

    def dismiss_pending(self, pending_id: str) -> None:
        item = self.state.find_pending(pending_id)
        if item is None:
            return
        contact_id = str(item.get("contact") or "")
        channel = str(item.get("channel") or "")
        pipe = self.hub.pipeline(channel)
        if pipe is not None:
            pipe.release_contact(contact_id)
        self.state.remove_pending(pending_id)
        self._on_changed()
