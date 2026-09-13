from __future__ import annotations
from collections import deque
from datetime import date
from pathlib import Path
import threading
import time
from typing import Callable
from widget.config import WidgetConfig, save_config
from widget.models import InboundMsg
from widget.scope import conversation_key

class RuntimeState:
    def __init__(self, cfg: WidgetConfig, today_fn: Callable[[], object] = date.today,
                 config_path: str | Path | None = None):
        self.cfg = cfg
        self.hook_ok = False
        self.backend_ok = False
        self.self_wxid = ""
        self.self_wxid_by_channel: dict[str, str] = {}
        self.hook_error = ""
        self.backend_error = ""
        self.received = 0
        self.sent = 0
        self.recv_by_channel: dict[str, int] = {}
        self.sent_by_channel: dict[str, int] = {}
        self.ai_enabled_by_channel: dict[str, bool] = {}
        self._today_fn = today_fn
        self._day = today_fn()
        self._recent: deque[dict] = deque(maxlen=20)
        self.pending: list[dict] = []
        self._pending_lock = threading.RLock()
        self._pending_sequence = 0
        self._conversation_listener = lambda _event: None
        self.limiter = None    # build_pipeline 会注入，用于「解除每日上限」即时生效
        self._config_path = Path(config_path) if config_path else None

    def _persist_config(self) -> None:
        """持久化用户在设置页/会话页做的选择；无路径的测试状态保持纯内存。"""
        if self._config_path is not None:
            save_config(self.cfg, self._config_path)

    @property
    def recent(self) -> list[dict]:
        return list(self._recent)

    def _roll_day(self) -> None:
        # "今日 收/发" 按自然日归零（否则跨天仍进程累计，显示不准）。
        d = self._today_fn()
        if d != self._day:
            self._day = d
            self.received = 0
            self.sent = 0

    def today_counts(self) -> tuple[int, int]:
        """返回当日 (收, 发)；读取即结算跨天归零。"""
        self._roll_day()
        return self.received, self.sent

    def set_self_wxid(self, channel_key: str, wxid: str) -> None:
        self.self_wxid_by_channel[channel_key] = wxid
        self.self_wxid = wxid

    def get_self_wxid(self, channel_key: str) -> str:
        return self.self_wxid_by_channel.get(channel_key, "")

    def set_ai_enabled(self, channel_key: str, enabled: bool) -> None:
        self.ai_enabled_by_channel[channel_key] = enabled

    def is_ai_enabled(self, channel_key: str) -> bool:
        return self.ai_enabled_by_channel.get(channel_key, True)

    def record_inbound(
        self, msg: InboundMsg, action: str, *, count_received: bool = True,
    ) -> None:
        self._roll_day()
        ck = msg.get("channel", "")
        if count_received:
            self.received += 1
            self.recv_by_channel[ck] = self.recv_by_channel.get(ck, 0) + 1
        if action in {"auto_reply", "handoff_notified"}:
            self.sent += 1
            self.sent_by_channel[ck] = self.sent_by_channel.get(ck, 0) + 1
        if count_received or action in {"auto_reply", "handoff_notified"}:
            self._recent.append({
                "contact": msg["contact_id"], "text": msg["text"], "action": action,
            })

    def add_pending(self, msg: InboundMsg, result: dict) -> bool:
        channel = msg.get("channel", "")
        contact = msg["contact_id"]
        msg_id = str(msg.get("msg_id") or "")
        with self._pending_lock:
            if msg_id:
                pending_id = f"{channel}|{msg_id}"
            else:
                self._pending_sequence += 1
                pending_id = f"{channel}|{contact}|pending-{self._pending_sequence}"
            item = {
                "id": pending_id,
                "msg_id": msg_id,
                "contact": contact,
                "contact_id": contact,
                "sender_id": msg.get("sender_id", contact),
                "text": msg["text"],
                "timestamp": int(msg.get("timestamp") or 0),
                "is_group": bool(msg.get("is_group", False)),
                "at_me": bool(msg.get("at_me", False)),
                "draft": result.get("reply_text", ""),
                "channel": channel,
                "kind": result.get("pending_kind", "handoff"),
            }
            existing = next((p for p in self.pending if p.get("id") == pending_id), None)
            if existing is not None:
                existing.update(item)
                return False
            # 一条客户消息就是一个待办；不能按联系人合并，否则回复其中一条会误清其它问题。
            self.pending.append(item)
            return True

    def remove_pending_for_message(self, msg: InboundMsg) -> bool:
        channel = str(msg.get("channel") or "")
        msg_id = str(msg.get("msg_id") or "")
        if not msg_id:
            return False
        return self.remove_pending(f"{channel}|{msg_id}")

    def pending_snapshot(self) -> list[dict]:
        with self._pending_lock:
            return [dict(item) for item in self.pending]

    def find_pending(self, pending_id: str) -> dict | None:
        with self._pending_lock:
            item = next((p for p in self.pending if p.get("id") == pending_id), None)
            return dict(item) if item is not None else None

    def has_uncertain_delivery(self, contact_id: str, channel: str | None = None) -> bool:
        with self._pending_lock:
            return any(
                str(item.get("contact") or "") == str(contact_id)
                and (channel is None or str(item.get("channel") or "") == str(channel))
                and item.get("kind") in {"delivery_uncertain", "delivery_waiting"}
                for item in self.pending
            )

    def remove_pending(self, pending_id: str) -> bool:
        """只完成一个明确的待办，绝不按客户批量清空。"""
        with self._pending_lock:
            before = len(self.pending)
            self.pending = [p for p in self.pending if p.get("id") != pending_id]
            return len(self.pending) != before

    def remove_pending_for_contact(self, contact_id: str, channel: str | None = None) -> None:
        """仅供关闭整段接待等显式会话级操作使用。"""
        with self._pending_lock:
            self.pending = [p for p in self.pending
                            if not (p.get("contact") == contact_id
                                    and (channel is None or p.get("channel", "") == channel))]

    def set_conversation_listener(self, callback) -> None:
        self._conversation_listener = callback or (lambda _event: None)

    def publish_outbound(self, msg: InboundMsg, text: str, provenance: str,
                         *, quote: bool = False) -> None:
        """发布即时出站气泡；仅待人工队列的人工回复可显式请求显示引用。"""
        if quote:
            from widget.reply_quote import delivered_text
            visible_text = delivered_text(text, msg)
        else:
            visible_text = text

        self._conversation_listener({
            "event_id": f"out:{msg.get('channel', '')}:{msg.get('msg_id', '')}:{provenance}",
            "direction": "outbound",
            "channel": msg.get("channel", ""),
            "contact_id": msg.get("contact_id", ""),
            "sender_id": self.get_self_wxid(msg.get("channel", "")) or self.self_wxid,
            "text": visible_text,
            "timestamp": int(time.time()),
            "is_group": bool(msg.get("is_group", False)),
            "provenance": provenance,
        })

    def record_agent_reply(self, contact_id: str, text: str) -> None:
        """记一条人工回复：计入今日发 + 最近列表。"""
        self._roll_day()
        self.sent += 1
        self._recent.append({"contact": contact_id, "text": text, "action": "agent_reply"})

    def toggle_auto_send(self) -> bool:
        self.cfg.auto_send = not self.cfg.auto_send
        self._persist_config()
        return self.cfg.auto_send

    def remove_daily_limit(self) -> None:
        self.cfg.daily_limit = 0
        if self.limiter is not None:
            self.limiter.daily_limit = 0

    def set_scope(self, **kw) -> None:
        for k, v in kw.items():
            setattr(self.cfg.scope, k, v)
        self._persist_config()

    def is_conversation_enabled(self, channel: str, contact_id: str) -> bool:
        key = conversation_key(channel, contact_id)
        scope = self.cfg.scope
        if key in scope.conversation_blocklist or contact_id in scope.contact_blocklist:
            return False
        if scope.private_mode == "selected":
            return key in scope.conversation_allowlist
        return True

    def set_conversation_enabled(self, channel: str, contact_id: str, enabled: bool) -> None:
        """控制未来消息是否进入客服链路；关闭后不调后端、不落后台、不通知。"""
        key = conversation_key(channel, contact_id)
        scope = self.cfg.scope
        if scope.private_mode == "selected":
            allowed = scope.conversation_allowlist
            if enabled and key not in allowed:
                allowed.append(key)
            elif not enabled:
                scope.conversation_allowlist = [x for x in allowed if x != key]
        else:
            blocked = scope.conversation_blocklist
            if enabled:
                scope.conversation_blocklist = [x for x in blocked if x != key]
            elif key not in blocked:
                blocked.append(key)
        if not enabled:
            self.remove_pending_for_contact(contact_id, channel)
        self._persist_config()

    def set_notifications(self, **kw) -> None:
        for k, v in kw.items():
            if hasattr(self.cfg.notifications, k):
                setattr(self.cfg.notifications, k, v)
        self._persist_config()
