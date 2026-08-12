from __future__ import annotations
import os
import tempfile
import time
from typing import Callable, Optional

_SUBDIR = "wecom_hook"

def status_path(pid: int, base_dir: Optional[str] = None) -> str:
    base = base_dir or tempfile.gettempdir()
    return os.path.join(base, _SUBDIR, f"status-{pid}.txt")

def _parse(text: str) -> dict:
    out: dict = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out

def read_status(pid: int, base_dir: Optional[str] = None) -> Optional[dict]:
    path = status_path(pid, base_dir)
    try:
        raw = _parse(open(path, "r", encoding="utf-8", errors="replace").read())
    except OSError:
        return None
    if "port" not in raw:
        return None
    try:
        port = int(raw["port"])
        fpid = int(raw.get("pid", pid))
    except ValueError:
        return None
    if fpid != pid:                      # 陈旧文件（同 pid 上次运行残留）→ 拒
        return None
    return {"port": port, "status": raw.get("status", ""),
            "pid": fpid, "proto": int(raw["proto"]) if raw.get("proto", "").isdigit() else 0}

def wait_ready(pid: int, timeout_s: float = 15.0, interval_s: float = 0.3,
               base_dir: Optional[str] = None,
               sleep: Callable[[float], None] = time.sleep,
               clock: Callable[[], float] = time.monotonic) -> Optional[int]:
    deadline = clock() + timeout_s
    while clock() < deadline:
        st = read_status(pid, base_dir)
        if st and st.get("status") == "ready":
            return st["port"]
        sleep(interval_s)
    return None
