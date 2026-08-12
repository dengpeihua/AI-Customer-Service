"""Small in-process task ledger for observable background work.

The ledger intentionally stores no message bodies, credentials, or arbitrary exception dumps. It is
diagnostic state for the current backend process, not a second business database.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from threading import Lock
from time import perf_counter
from uuid import uuid4


class OpsTaskRegistry:
    def __init__(self, max_items: int = 200) -> None:
        self._items: deque[dict] = deque(maxlen=max(10, int(max_items)))
        self._started: dict[str, float] = {}
        self._lock = Lock()

    def start(self, tenant_id: int, kind: str, label: str) -> str:
        task_id = uuid4().hex[:12]
        item = {
            "id": task_id,
            "tenant_id": int(tenant_id),
            "kind": str(kind)[:60],
            "label": str(label)[:120],
            "status": "running",
            "detail": "",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "duration_ms": None,
        }
        with self._lock:
            self._items.appendleft(item)
            self._started[task_id] = perf_counter()
        return task_id

    def finish(self, task_id: str, *, status: str, detail: str = "") -> None:
        resolved = status if status in {"completed", "failed", "cancelled"} else "failed"
        with self._lock:
            started = self._started.pop(task_id, None)
            for item in self._items:
                if item["id"] != task_id:
                    continue
                item["status"] = resolved
                item["detail"] = str(detail)[:240]
                item["finished_at"] = datetime.now(timezone.utc).isoformat()
                item["duration_ms"] = round((perf_counter() - started) * 1000, 1) if started else None
                break

    def recent(self, tenant_id: int, limit: int = 50) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self._items
                    if item["tenant_id"] == int(tenant_id)][:max(1, int(limit))]

    def delete(self, tenant_id: int, task_ids: list[str]) -> int:
        """Delete tenant-owned diagnostic records without cancelling running work."""
        wanted = {str(task_id) for task_id in task_ids if str(task_id)}
        if not wanted:
            return 0
        deleted = 0
        tenant_id = int(tenant_id)
        with self._lock:
            kept: deque[dict] = deque(maxlen=self._items.maxlen)
            for item in self._items:
                if item["tenant_id"] == tenant_id and item["id"] in wanted:
                    self._started.pop(item["id"], None)
                    deleted += 1
                else:
                    kept.append(item)
            self._items = kept
        return deleted


ops_registry = OpsTaskRegistry()
