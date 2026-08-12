"""入站消息通知策略。

策略与消息处理解耦：Pipeline 决定 auto_reply/handoff，本模块只决定是否通知客服。
同一联系人短时间连续消息按渠道去重，避免托盘气泡和前台抢焦点形成通知风暴。
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from widget.config import NotificationConfig


class NotificationGate:
    def __init__(self, config: NotificationConfig,
                 clock: Callable[[], float] = time.monotonic):
        self.config = config
        self._clock = clock
        self._last: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    def should_notify(self, action: str, channel: str, contact_id: str) -> bool:
        is_handoff = action in ("handoff", "error")
        if is_handoff:
            enabled = self.config.handoff
            kind = "handoff"
        elif action in ("auto_reply", "auto_reply_draft"):
            enabled = self.config.auto_reply
            kind = "auto_reply"
        else:
            return False
        if not enabled:
            return False

        now = self._clock()
        key = (kind, f"{channel}|{contact_id}")
        cooldown = max(0.0, float(self.config.cooldown_s))
        with self._lock:
            previous = self._last.get(key)
            if previous is not None and now - previous < cooldown:
                return False
            self._last[key] = now
        return True
