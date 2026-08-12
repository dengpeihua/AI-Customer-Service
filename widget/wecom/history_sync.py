"""WeComHistorySyncer —— 企微本地库历史【自动】反哺后台同步器。

用户要求：客户不该手动点「拉取企微历史」按钮，历史应自动、持续地同步进产品。
本组件就是那个后台自动化：一个守护线程，周期性地从 col_hook 的 `/collog`（经 adapter 的
`harvest_history_texts()`）收割企微本地库(message.db)解密后的历史聊天正文，攒够一批就自动送后端
LLM 蒸馏成 FAQ 反哺知识库（`backend.ingest_history_texts`）。全程无需任何人点击。

设计要点：
- **增量 + 跨轮去重**：MsgDbReader 自带增量游标；这里再用有界 seen 集去掉环形缓冲复现的重复。
- **节流**：攒够 `min_batch` 且距上次 flush 过了 `flush_interval_s` 才反哺一次（或积压过多强制 flush），
  避免频繁打 LLM 蒸馏端点烧钱。
- **best-effort**：col_hook 没接 / 后端挂了 / 这批没可提炼内容 → 安全跳过，绝不抛、绝不拖垮挂件。
  flush 失败即丢这批（已在 seen 里不会重复累积），换下一批；不做毒批无限重试。
- **只反哺 KB**：col_hook 收割的是**无角色、无联系人归属**的裸文本（见 docs 里的实测结论），
  没法重建 per-contact 气泡，所以喂给知识库（AI 的记忆），不进会话视图。会话视图由实时 recv/send 流填。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable

# 明显的系统/占位文案：反哺 KB 无意义，攒批前先滤掉，省 LLM 调用。
_SYSTEM_NOISE = frozenset({
    "以上是打招呼内容",
    "对方正在输入",
    "消息已发出，但被对方拒收了。",
})


class WeComHistorySyncer:
    def __init__(self, adapter, backend, *, poll_interval_s: float = 20.0,
                 flush_interval_s: float = 300.0, min_batch: int = 6,
                 seen_max: int = 5000, logger: Callable[[str], None] | None = None):
        self._adapter = adapter                # 需有 harvest_history_texts() -> list[str]
        self._backend = backend                # 需有 ingest_history_texts(texts, title) -> dict
        self._poll_interval_s = poll_interval_s
        self._flush_interval_s = flush_interval_s
        self._min_batch = max(1, min_batch)
        self._seen_max = seen_max
        self._log = logger or (lambda m: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._seen: set[str] = set()           # 跨轮去重（有界）
        self._seen_order: deque[str] = deque()
        self._pending: list[str] = []          # 待反哺
        self._last_flush = 0.0                  # monotonic
        self._synced = 0                        # 已反哺条数（GUI 显示）
        self._batches = 0                       # 已反哺批次
        self._last_error = ""

    # ---- 生命周期 ----
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._last_flush = time.monotonic()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="wecom-history-sync")
        self._thread.start()
        self._log("企微历史自动同步：已启动")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def status(self) -> dict:
        """供 GUI 显示自动同步状态（不用客户点任何东西）。"""
        with self._lock:
            return {"running": self._thread is not None and not self._stop.is_set(),
                    "synced": self._synced, "batches": self._batches,
                    "pending": len(self._pending), "last_error": self._last_error}

    # ---- 内部 ----
    def _remember(self, text: str) -> bool:
        """记入有界 seen；返回 True=首次见（应处理）。"""
        if text in self._seen:
            return False
        if len(self._seen_order) >= self._seen_max:
            old = self._seen_order.popleft()
            self._seen.discard(old)
        self._seen.add(text)
        self._seen_order.append(text)
        return True

    def _harvest_once(self) -> None:
        try:
            texts = self._adapter.harvest_history_texts()
        except Exception as e:                 # adapter 无 reader / 桥挂 → 跳过
            self._last_error = f"harvest: {e}"
            return
        fresh = []
        for t in texts or []:
            s = (t or "").strip()
            if s and s not in _SYSTEM_NOISE and self._remember(s):
                fresh.append(s)
        if fresh:
            with self._lock:
                self._pending.extend(fresh)

    def _should_flush(self, now: float) -> bool:
        with self._lock:
            n = len(self._pending)
        if n < self._min_batch:
            return False
        # 攒够且过了节流间隔；或积压过多（>8×min_batch）不再等，立刻 flush 防内存长
        return (now - self._last_flush) >= self._flush_interval_s or n >= self._min_batch * 8

    def _flush(self) -> None:
        with self._lock:
            batch = list(self._pending)
        if not batch:
            return
        try:
            out = self._backend.ingest_history_texts(batch, "企微本地历史（自动同步）")
        except Exception as e:
            # 后端挂/这批没可提炼内容(400) → 丢这批（已在 seen，不会重复累积），不毒批重试
            self._last_error = f"flush: {e}"
            with self._lock:
                self._pending = [t for t in self._pending if t not in set(batch)]
                self._last_flush = time.monotonic()
            self._log(f"企微历史自动同步：本批 {len(batch)} 条未入库（{e}），跳过")
            return
        with self._lock:
            done = set(batch)
            self._pending = [t for t in self._pending if t not in done]
            self._synced += len(batch)
            self._batches += 1
            self._last_flush = time.monotonic()
            self._last_error = ""
        self._log(f"企微历史自动同步：反哺 {len(batch)} 条 → 知识库（{out.get('title', '') or '完成'}）")

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._harvest_once()
            if self._should_flush(time.monotonic()):
                self._flush()
            self._stop.wait(self._poll_interval_s)
        # 退出前把最后一批也 flush 掉（best-effort）
        try:
            with self._lock:
                has = len(self._pending) >= self._min_batch
            if has:
                self._flush()
        except Exception:
            pass
