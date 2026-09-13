"""M3 转人工控制器：把「待人工」队列变成可操作的动作（多渠道）。

人工在转人工浮窗里对某条待办：
- reply_pending(pending_id, text)：按待办绑定的 channel 发出带原消息引用的回复；成功后只移除
  这个 pending_id，其他同客户消息继续保留。
- dismiss_pending(pending_id)：不回复，只标记这一条处理完。
- send(contact, text, channel)：普通人工发送，不隐式清空任何待办。

多个抖音账号按 channel 回到各自适配器，绝不串台。与 GUI 解耦，便于单测。
"""
from __future__ import annotations

from widget.reply_quote import send_reply


class HandoffController:
    def __init__(self, hub, state, on_changed=None):
        self.hub = hub                  # ChannelHub：按 channel 路由 adapter/pipeline
        self.state = state              # RuntimeState（pending / 计数）
        self._on_changed = on_changed or (lambda: None)
        self.last_error = ""

    def set_on_changed(self, callback) -> None:
        self._on_changed = callback or (lambda: None)

    def pending(self) -> list[dict]:
        snapshot = getattr(self.state, "pending_snapshot", None)
        return snapshot() if callable(snapshot) else list(self.state.pending)

    def _send(self, contact_id: str, text: str, channel: str | None,
              reply_to: dict | None) -> bool:
        self.last_error = ""
        if not text or not text.strip():
            self.last_error = "回复内容为空"
            return False
        checker = getattr(self.state, "has_uncertain_delivery", None)
        if callable(checker) and checker(contact_id, channel):
            self.last_error = "上一条消息的投递结果仍在确认中，为避免重复发送已暂停"
            return False
        adapter = self.hub.adapter(channel)
        if adapter is None:
            self.last_error = "找不到该待办对应的抖音账号通道"
            return False
        try:
            res = send_reply(adapter, contact_id, text, reply_to, provenance="human")
        except Exception as exc:  # noqa: BLE001 - surface the channel failure in the UI
            self.last_error = str(exc)[:500] or "渠道发送异常"
            return False
        if not res.ok:
            self.last_error = str(getattr(res, "error", "") or "渠道未返回成功回执")[:500]
            return False
        self.state.record_agent_reply(contact_id, text)
        if reply_to:
            self.state.publish_outbound(reply_to, text, "agent", quote=True)
        return True

    def send(self, contact_id: str, text: str, channel: str | None = None) -> bool:
        """发送普通人工消息；公开入口没有引用参数，确保非待办消息永不引用。"""
        return self._send(contact_id, text, channel, reply_to=None)

    def reply_pending(self, pending_id: str, text: str, *, notify: bool = True) -> bool:
        self.last_error = ""
        item = self.state.find_pending(pending_id)
        if item is None:
            self.last_error = "这条待办已不存在，请刷新列表"
            return False
        if item.get("kind") in {"delivery_uncertain", "delivery_waiting"}:
            self.last_error = "上一条消息的投递结果仍在确认中，为避免重复发送已暂停"
            return False
        contact_id = str(item.get("contact") or "")
        channel = str(item.get("channel") or "")
        if not self._send(contact_id, text, channel, reply_to=item):
            return False                # 发送失败：只保留这一条待办，人工可稍后重试
        pipe = self.hub.pipeline(channel)
        if pipe is not None:
            pipe.release_contact(contact_id)
        self.state.remove_pending(pending_id)
        if notify:
            self.notify_changed()
        return True

    def notify_changed(self) -> None:
        """Notify UI observers after the caller has returned to the GUI thread."""
        self._on_changed()

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
