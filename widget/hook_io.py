"""Serialize calls into the personal-WeChat hook.

The bundled hook executes database work inside Weixin.exe. Its HTTP server can
accept concurrent requests, but the underlying WCDB/SQLite bridge is not safe to
run concurrently. A lock per hook port keeps polling, history refreshes and
readiness probes from entering that native bridge at the same time.
"""
from __future__ import annotations

import threading


_registry_guard = threading.Lock()
_port_locks: dict[int, threading.RLock] = {}


def lock_for_port(port: int) -> threading.RLock:
    """Return the process-wide re-entrant lock for one local hook port."""
    key = int(port)
    if not 1 <= key <= 65535:
        raise ValueError(f"invalid hook port: {port}")
    with _registry_guard:
        lock = _port_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _port_locks[key] = lock
        return lock
