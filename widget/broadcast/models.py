from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class BroadcastTask:
    id: str
    template: str
    recipients: list[str]
    scheduled_at: float | None = None
    status: str = "draft"                 # draft|scheduled|running|paused|done|cancelled
    sent: list[str] = field(default_factory=list)
    failed: list[list] = field(default_factory=list)   # [[wxid, reason], ...]
    created_at: float = 0.0
    throttle: dict = field(default_factory=dict)
    processed_at: dict = field(default_factory=dict)   # wxid -> epoch 秒：该收件人被发送/判失败的时刻


def new_task(template: str, recipients: list[str], scheduled_at: float | None = None,
             throttle: dict | None = None) -> BroadcastTask:
    return BroadcastTask(
        id=uuid.uuid4().hex,
        template=template,
        recipients=list(recipients),
        scheduled_at=scheduled_at,
        status="scheduled" if scheduled_at is not None else "draft",
        created_at=time.time(),
        throttle=throttle or {},
    )


def load_tasks(path: str | Path) -> list[BroadcastTask]:
    p = Path(path)
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    fields = set(BroadcastTask.__dataclass_fields__)
    return [BroadcastTask(**{k: v for k, v in d.items() if k in fields}) for d in raw]


def save_tasks(tasks: list[BroadcastTask], path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps([asdict(t) for t in tasks], ensure_ascii=False),
                   encoding="utf-8")
    os.replace(tmp, p)   # 原子替换，避免写一半崩掉留下损坏文件


_upsert_lock = threading.Lock()


def upsert_task(path: str | Path, task: BroadcastTask) -> None:
    """按 id 单条 read-modify-write，绝不用调用方手里的整表快照覆盖别人已落盘的进展。

    多个执行者（UI 发送线程、定时调度线程）可能各自持有各自 load 出来的任务表快照、
    并发推进各自的任务；若各自 save_tasks(整表) 回写，后写的一方会用自己手里的旧快照
    把对方刚落盘的最新状态（如 done）打回旧状态，导致任务被"复活"重发。
    upsert_task 只替换/追加自己这一条，用锁保证同进程内的 read-modify-write 是原子的。
    """
    with _upsert_lock:
        tasks = load_tasks(path)
        for i, t in enumerate(tasks):
            if t.id == task.id:
                tasks[i] = task
                break
        else:
            tasks.append(task)
        save_tasks(tasks, path)


def delete_task(path: str | Path, task_id: str) -> None:
    """按 id 从记录里删除一条任务（找不到则无操作）。与 upsert 共用锁，读改写原子。"""
    with _upsert_lock:
        tasks = [t for t in load_tasks(path) if t.id != task_id]
        save_tasks(tasks, path)
