from __future__ import annotations

from widget.broadcast.models import BroadcastTask


def due_tasks(tasks: list[BroadcastTask], now: float) -> list[BroadcastTask]:
    return [t for t in tasks
            if t.status == "scheduled" and t.scheduled_at is not None and t.scheduled_at <= now]
