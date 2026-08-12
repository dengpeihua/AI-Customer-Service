# widget/wechat/ports.py
"""个人微信 hook 端口发现与分配 —— 逐 pid 问操作系统要监听端口。

M4：N 个 `Weixin.exe` 各带 `StartPort=<port>` 启动，注入的 hook 各自绑不同端口。
Python 因此不再假设 30001，而是对每个 Weixin 进程问「你在听哪个端口」。

用 psutil 的**逐进程** `net_connections()`（实测本机 260 进程 0 个 AccessDenied，
无需管理员），而不是系统级 `psutil.net_connections()` —— 后者在部分 Windows 环境需要提权。

区间外的监听端口一律忽略：微信自身还有别的监听端口，误认会连错服务。
"""
from __future__ import annotations

import socket
from typing import Callable, Iterable, Optional

import psutil

PORT_BASE = 30001
PORT_SLOTS = 16
PORT_MAX = PORT_BASE + PORT_SLOTS - 1
WECHAT_PROCESS_NAME = "Weixin.exe"


def _default_listen_ports(pid: int) -> list[int]:
    """该 pid 上所有 LISTEN 中的 TCP 端口；取不到（进程没了/无权限）返回空表。"""
    try:
        conns = psutil.Process(pid).net_connections(kind="tcp")
    except Exception:                                   # noqa: BLE001 —— 探测失败不该崩挂件
        return []
    out: list[int] = []
    for c in conns:
        try:
            if c.status == "LISTEN" and c.laddr:
                out.append(int(c.laddr.port))
        except Exception:                               # noqa: BLE001
            continue
    return out


def listening_port_for_pid(
    pid: int,
    lo: int = PORT_BASE,
    hi: int = PORT_MAX,
    *,
    listen_ports: Optional[Callable[[int], list[int]]] = None,
) -> Optional[int]:
    """该 pid 的 hook 端口（落在 [lo, hi] 的最小监听端口）；没有返回 None。"""
    fn = listen_ports or _default_listen_ports
    in_range = sorted(p for p in fn(pid) if lo <= p <= hi)
    return in_range[0] if in_range else None


def listening_pids_for_port(
    port: int,
    *,
    connections: Optional[Callable[[], list]] = None,
) -> list[int]:
    """精确列出占用同一 hook 端口的全部 pid；探测失败返回空表（失败关闭）。

    不能把「端口有监听」简化成单个 pid：hook 使用端口复用时，两台 Weixin.exe 可以同时
    LISTEN 30001，后续 HTTP 请求会被系统分发给不同账号。此时任何一个 adapter 都不拥有一个
    稳定的账号边界，必须拒绝收发，直到每个微信实例使用唯一端口。
    """
    try:
        conns = (connections or (lambda: psutil.net_connections(kind="tcp")))()
    except Exception:                                   # noqa: BLE001
        return []
    owners: set[int] = set()
    for conn in conns:
        try:
            if conn.status == "LISTEN" and conn.laddr \
                    and int(conn.laddr.port) == int(port) and conn.pid:
                owners.add(int(conn.pid))
        except Exception:                               # noqa: BLE001
            continue
    return sorted(owners)


def pid_still_owns_port(pid: int, port: int, *, port_for_pid=None, owners_for_port=None) -> bool:
    """那台微信是不是**还在、且还占着同一个端口**。查不出来一律返回 False（fail closed）。

    ★唯一实现★：`widget/app.py::pid_still_owns_port`（重连/掉线自愈）与
    `WeChatHookAdapter`（每次收发前的归属闸）都调这一个，别再抄第二份。

    为什么这是安全判据而不是优化：adapter 手里只有端口，端口易主它不知道。微信重启会换 pid，
    而端口是从 30001 起分配的 —— 甲的微信一退，同一个 30001 立刻可能属于**另一个已配置账号**
    的微信（客户按文档跑 `pythonw start_wechat.py`，不带 --port 就是绑 30001）。
    """
    pid, port = int(pid or 0), int(port or 0)
    if not pid or not port:
        return False                       # 从没认领到过 pid/端口 → 无从自证，当作已失效
    try:
        if int((port_for_pid or listening_port_for_pid)(pid) or 0) != port:
            return False
        owners = (owners_for_port or listening_pids_for_port)(port)
        return owners == [pid]              # 同端口多 pid = 请求会串账号，绝不能声称“仍归自己”
    except Exception:                      # noqa: BLE001 —— 探测失败 = 无法自证 = 视为易主
        return False


def _default_pids(process_name: str) -> list[int]:
    out: list[int] = []
    target = process_name.lower()
    for p in psutil.process_iter(["pid", "name"]):
        try:
            if (p.info.get("name") or "").lower() == target:
                out.append(int(p.info["pid"]))
        except Exception:                               # noqa: BLE001 —— 单进程读不到就跳过
            continue
    return out


def wechat_pids(
    process_name: str = WECHAT_PROCESS_NAME,
    *,
    pids_fn: Optional[Callable[[str], list[int]]] = None,
) -> list[int]:
    """机器上全部 Weixin.exe 的 pid（升序，顺序稳定便于日志/测试）。"""
    return sorted((pids_fn or _default_pids)(process_name))


def _default_port_free(port: int) -> bool:
    """端口是否空闲。绑 "" (所有网卡)——hook 绑的是 0.0.0.0，只探 127.0.0.1 会漏判。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", port))
        return True
    except OSError:
        return False


def allocate_port(
    lo: int = PORT_BASE,
    hi: int = PORT_MAX,
    *,
    is_free: Optional[Callable[[int], bool]] = None,
    skip: Iterable[int] = (),
) -> Optional[int]:
    """给新实例挑一个空闲的 hook 端口；区间用尽返回 None。"""
    free = is_free or _default_port_free
    skipped = set(skip)
    for port in range(lo, hi + 1):
        if port in skipped:
            continue
        if free(port):
            return port
    return None
