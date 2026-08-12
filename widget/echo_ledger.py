from __future__ import annotations

import time
from collections import deque
from typing import Callable


class EchoLedger:
    """挂件"自己刚发出去的话"账本 —— 一处真相同时服务两件事：

    ① 回声抑制：收到自己刚发的原样消息即丢，防自问自答死循环（即使发信人 wxid 解析出错也能靠内容挡）。
    ② 消息 AI 溯源打标（provenance）：这条出向消息是 ai / human / broadcast 谁发的——**发时即知**，
       取代挂件聊天页原先"拿收到的文本去后端 AI 回复集/群发记录里事后猜"的脆弱做法。

    有界（超 max_items 淘汰最旧，dict+deque 同步淘汰防泄漏）
    + TTL（超时不再用于抑制/打标：否则"客户过一阵正好说了同一句话"会被永久误判成回声/AI）。
    """

    def __init__(self, max_items: int = 200, ttl_s: float = 6 * 3600,
                 clock: Callable[[], float] = time.monotonic):
        self._items: dict[str, dict] = {}          # key -> {"source": str, "ts": float}
        self._order: deque[str] = deque()
        self._max = max_items
        self._ttl_s = ttl_s
        self._clock = clock

    @staticmethod
    def _key(text: str) -> str:
        return (text or "").strip()

    def remember(self, text: str, source: str = "human") -> None:
        key = self._key(text)
        if not key:
            return
        now = self._clock()
        if key in self._items:                     # 已在账本：刷新来源+时间，不重复入队
            self._items[key] = {"source": source, "ts": now}
            return
        self._items[key] = {"source": source, "ts": now}
        self._order.append(key)
        while len(self._order) > self._max:         # 有界淘汰最旧
            old = self._order.popleft()
            self._items.pop(old, None)

    def _fresh(self, key: str) -> dict | None:
        item = self._items.get(key)
        if item is None:
            return None
        if self._clock() - item["ts"] > self._ttl_s:
            return None                             # 过期
        return item

    def is_own(self, text: str) -> bool:
        """是不是自己刚发出去的（未过期）→ 回声抑制用。"""
        return self._fresh(self._key(text)) is not None

    def source_of(self, text: str) -> str | None:
        """这条出向文本的来源（ai/human/broadcast），未知/过期返回 None（聊天页据此显示"人工"）。"""
        item = self._fresh(self._key(text))
        return item["source"] if item else None
