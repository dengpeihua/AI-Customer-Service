from __future__ import annotations
import threading
import time
import random
from collections import deque
from datetime import date
from typing import Callable
from widget.config import WidgetConfig
from widget.adapters.base import ChannelAdapter


class RateLimiter:
    def __init__(self, rate_per_min: int, daily_limit: int,
                 clock: Callable[[], float] = time.monotonic,
                 today_fn: Callable[[], object] = date.today):
        self.rate_per_min = rate_per_min
        self.daily_limit = daily_limit
        self._clock = clock
        self._today_fn = today_fn
        self._recent: deque[float] = deque()
        self._day_count = 0
        self._day = today_fn()
        self._lock = threading.Lock()   # 页面发送线程 + 定时调度线程可能并发调用，限速计数不能错乱

    def _roll_day(self) -> None:
        # 日额度按自然日归零；跨天则重置计数（否则触顶后进程不重启就永久降级）。
        d = self._today_fn()
        if d != self._day:
            self._day = d
            self._day_count = 0

    def can_send(self) -> bool:
        with self._lock:
            self._roll_day()
            now = self._clock()
            while self._recent and now - self._recent[0] >= 60:
                self._recent.popleft()
            if len(self._recent) >= self.rate_per_min:
                return False
            if self.daily_limit and self._day_count >= self.daily_limit:
                return False
            return True

    def record(self) -> None:
        with self._lock:
            self._roll_day()
            self._recent.append(self._clock())
            self._day_count += 1


class Sender:
    def __init__(self, adapter: ChannelAdapter, cfg: WidgetConfig, limiter: RateLimiter,
                 sleep_fn: Callable[[float], None] = time.sleep,
                 rand_fn: Callable[[float, float], float] = random.uniform):
        self.adapter = adapter
        self.cfg = cfg
        self.limiter = limiter
        self._sleep = sleep_fn
        self._rand = rand_fn
        self.last_result = None

    def deliver(self, contact_id: str, text: str) -> bool:
        """发送 AI/系统自动消息；自动通道永远不携带客户消息引用。"""
        self.last_result = None
        if not self.cfg.auto_send:
            return False
        if not self.limiter.can_send():
            return False
        delay = self._rand(self.cfg.send_delay_min_s, self.cfg.send_delay_max_s)
        self._sleep(delay)
        res = self.adapter.send_message(contact_id, text, provenance="ai")
        self.last_result = res
        if res.ok or bool(getattr(res, "uncertain", False)):
            # Once a browser side effect may have started, conservatively consume
            # quota even when the new message bubble could not be confirmed.
            self.limiter.record()
        if res.ok:
            return True
        return False

    def reconcile_delivery(self, contact_id: str, text: str, since_ts: int) -> str:
        reconcile = getattr(self.adapter, "reconcile_delivery", None)
        if callable(reconcile):
            try:
                status = str(reconcile(contact_id, text, since_ts))
            except Exception:
                return "unknown"
            return status if status in {"delivered", "not_delivered", "unknown"} else "unknown"
        return "delivered" if self.was_delivered_since(contact_id, text, since_ts) else "not_delivered"

    def was_delivered_since(self, contact_id: str, text: str, since_ts: int) -> bool:
        """Reconcile an expired delivery lease against authoritative enterprise chat history."""
        read = getattr(self.adapter, "read_conversation", None)
        if read is None:
            return False
        try:
            messages = read(contact_id)
        except Exception:
            return False
        threshold = max(0, int(since_ts or 0) - 5)
        return any(
            bool(message.get("is_self"))
            and str(message.get("text") or "") == str(text)
            and int(message.get("ts") or 0) >= threshold
            for message in messages or [] if isinstance(message, dict)
        )
