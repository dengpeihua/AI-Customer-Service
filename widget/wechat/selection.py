"""同端口多微信的显式选择、窗口预览与安全可达性调整。

两个 Weixin.exe 可以借助 SO_REUSEADDR 同时监听 30001。旧版排他/重开辅助函数仍为兼容诊断
保留；生产启动选择流程不退出也不重开任何微信，只使用连接级 PID 校验，确保业务数据不会送到
另一个账号。目标 PID 暂时不可达时保持失败关闭，并由运行时继续重试。
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from dataclasses import dataclass, replace
from pathlib import Path, PureWindowsPath
from typing import Callable, Iterable, Optional

import psutil


@dataclass(frozen=True)
class WechatProcessChoice:
    pid: int
    wxid: str
    account_label: str
    window_title: str = "微信"
    hook_port: int = 0


def wxid_from_open_paths(paths: Iterable[str]) -> str:
    """从 ``...\\xwechat_files\\<wxid>_<随机后缀>\\...`` 提取当前登录账号。"""
    for raw in paths or ():
        try:
            parts = PureWindowsPath(str(raw).replace("/", "\\")).parts
        except (TypeError, ValueError):
            continue
        for index, part in enumerate(parts[:-1]):
            if part.lower() != "xwechat_files":
                continue
            account_dir = parts[index + 1]
            if account_dir.casefold() in {"all_users", "public_users", "shared_users"}:
                continue
            if "_" not in account_dir:
                continue
            wxid, suffix = account_dir.rsplit("_", 1)
            if wxid and len(suffix) >= 4:
                return wxid
    return ""


def _open_paths(pid: int) -> list[str]:
    try:
        return [item.path for item in psutil.Process(int(pid)).open_files()]
    except Exception:  # noqa: BLE001 - 进程可能恰好退出/无权限
        return []


def pick_wechat_window(candidates: Iterable[tuple[int, str, bool]]) -> tuple[int, str]:
    """优先可见主窗；微信缩到托盘时回退到标题为“微信/Weixin”的隐藏主窗。"""
    rows = [(int(hwnd), str(title or "").strip(), bool(visible))
            for hwnd, title, visible in candidates if int(hwnd or 0)]
    visible = next(((hwnd, title) for hwnd, title, shown in rows if shown and title), None)
    if visible:
        return visible
    names = {"微信", "weixin", "wechat"}
    hidden = next(((hwnd, title) for hwnd, title, _shown in rows
                   if title.casefold() in names), None)
    return hidden or (0, "")


def _main_window(pid: int) -> tuple[int, str]:
    if not hasattr(ctypes, "windll"):
        return 0, ""
    user32 = ctypes.windll.user32
    found: list[tuple[int, str, bool]] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int

    @callback_type
    def visit(hwnd, _lparam):
        owner = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if int(owner.value) != int(pid):
            return True
        length = int(user32.GetWindowTextLengthW(hwnd) or 0)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, len(buffer))
        title = buffer.value.strip()
        if title:
            found.append((int(hwnd), title, bool(user32.IsWindowVisible(hwnd))))
        return True

    try:
        user32.EnumWindows(visit, 0)
    except Exception:  # noqa: BLE001
        return 0, ""
    return pick_wechat_window(found)


def discover_wechat_choices(
    pids: Iterable[int], *,
    paths_fn: Optional[Callable[[int], list[str]]] = None,
    window_fn: Optional[Callable[[int], tuple[int, str]]] = None,
) -> list[WechatProcessChoice]:
    choices: list[WechatProcessChoice] = []
    for pid in sorted({int(value) for value in pids if int(value or 0)}):
        wxid = wxid_from_open_paths((paths_fn or _open_paths)(pid))
        _hwnd, title = (window_fn or _main_window)(pid)
        label = f"微信账号 {wxid}" if wxid else f"微信进程 PID {pid}（账号识别中）"
        choices.append(WechatProcessChoice(pid, wxid, label, title or "微信"))
    return choices


def focus_wechat_process(pid: int) -> bool:
    hwnd, _title = _main_window(pid)
    if not hwnd or not hasattr(ctypes, "windll"):
        return False
    try:
        user32 = ctypes.windll.user32
        user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindowAsync.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.BringWindowToTop.argtypes = [wintypes.HWND]
        user32.BringWindowToTop.restype = wintypes.BOOL
        user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.SetWindowPos.restype = wintypes.BOOL
        user32.SwitchToThisWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
        user32.SwitchToThisWindow.restype = None
        user32.GetForegroundWindow.restype = wintypes.HWND

        user32.ShowWindowAsync(hwnd, 9)  # SW_RESTORE
        # SetForegroundWindow 受 Windows 前台焦点锁限制；SwitchToThisWindow 用于用户显式点击
        # “查看窗口”的场景，可把目标任务窗口真正切到前台，而不仅仅是改变 Z 序。
        user32.SwitchToThisWindow(hwnd, True)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)

        # Windows 的 foreground-lock 可能拒绝 SetForegroundWindow。短暂置顶再立即还原不会让
        # 微信长期悬浮，却能保证它确实出现在已最小化的选择器前面。
        flags = 0x0001 | 0x0002 | 0x0040  # SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW
        topmost = wintypes.HWND(-1)
        not_topmost = wintypes.HWND(-2)
        user32.SetWindowPos(hwnd, topmost, 0, 0, 0, 0, flags)
        user32.SetWindowPos(hwnd, not_topmost, 0, 0, 0, 0, flags)
        if int(user32.GetForegroundWindow() or 0) != hwnd:
            user32.SwitchToThisWindow(hwnd, True)
        return int(user32.GetForegroundWindow() or 0) == hwnd
    except Exception:  # noqa: BLE001
        return False


def stop_wechat_process(pid: int, *, graceful_timeout_s: float = 1.5) -> bool:
    """退出未选中的冲突微信；先请求关窗，仍存活才终止进程。"""
    try:
        process = psutil.Process(int(pid))
    except psutil.Error:
        return True
    hwnd, _title = _main_window(pid)
    if hwnd and hasattr(ctypes, "windll"):
        try:
            user32 = ctypes.windll.user32
            user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                            wintypes.WPARAM, wintypes.LPARAM]
            user32.PostMessageW.restype = wintypes.BOOL
            user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
            process.wait(timeout=max(0.0, graceful_timeout_s))
            return True
        except psutil.TimeoutExpired:
            pass
        except Exception:  # noqa: BLE001
            pass
    try:
        process.terminate()
        process.wait(timeout=5)
        return True
    except psutil.NoSuchProcess:
        return True
    except Exception:  # noqa: BLE001
        return False


def choose_exclusive_wechat_process(
    port: int, *,
    chooser: Callable[[list[WechatProcessChoice]], Optional[WechatProcessChoice]],
    owners_fn: Optional[Callable[[int], list[int]]] = None,
    choices_fn: Optional[Callable[[list[int]], list[WechatProcessChoice]]] = None,
    stop_fn: Optional[Callable[[int], bool]] = None,
    sleep_fn: Optional[Callable[[float], None]] = None,
    attempts: int = 20,
) -> tuple[Optional[WechatProcessChoice], str]:
    """让用户选择一个进程，并确认它成为 `port` 的唯一监听者。"""
    if owners_fn is None:
        from widget.wechat.ports import listening_pids_for_port
        owners_fn = listening_pids_for_port
    owners = list(owners_fn(int(port)) or [])
    if len(owners) <= 1:
        return None, ""
    choices = (choices_fn or discover_wechat_choices)(owners)
    selected = chooser(choices)
    if selected is None:
        return None, "用户取消了个人微信接管选择"
    if selected.pid not in owners:
        return None, "所选微信进程已变化，请重新选择"
    if not selected.wxid:
        return None, "无法识别所选微信的账号目录，为防止接错账号已取消接管"

    stopper = stop_fn or stop_wechat_process
    for pid in owners:
        if pid != selected.pid:
            stopper(pid)

    nap = sleep_fn or time.sleep
    current = owners
    for _ in range(max(1, int(attempts))):
        current = list(owners_fn(int(port)) or [])
        if current == [selected.pid]:
            return selected, ""
        nap(0.25)
    if selected.pid not in current:
        return None, "所选微信已退出，未建立接管"
    return None, f"端口 {port} 仍被多个微信进程占用，未建立接管"


def choose_shared_port_wechat_process(
    port: int,
    *,
    chooser: Callable[[list[WechatProcessChoice]], Optional[WechatProcessChoice]],
    owners_fn: Optional[Callable[[int], list[int]]] = None,
    choices_fn: Optional[Callable[[list[int]], list[WechatProcessChoice]]] = None,
) -> tuple[Optional[WechatProcessChoice], str]:
    """选择共享端口上的账号，但不结束任何微信；后续由连接级 PID 路由隔离请求。"""
    if owners_fn is None:
        from widget.wechat.ports import listening_pids_for_port
        owners_fn = listening_pids_for_port
    owners = list(owners_fn(int(port)) or [])
    if len(owners) <= 1:
        return None, ""
    choices = (choices_fn or discover_wechat_choices)(owners)
    selected = chooser(choices)
    if selected is None:
        return None, "用户取消了个人微信接管选择"
    if selected.pid not in owners:
        return None, "所选微信进程已变化，请重新选择"
    if not selected.wxid:
        return None, "无法识别所选微信的账号目录，为防止接错账号已取消接管"
    return selected, ""


def choose_hooked_wechat_process(
    default_port: int,
    *,
    chooser: Callable[[list[WechatProcessChoice]], Optional[WechatProcessChoice]],
    pids_fn: Optional[Callable[[], list[int]]] = None,
    port_fn: Optional[Callable[[int], Optional[int]]] = None,
    choices_fn: Optional[Callable[[list[int]], list[WechatProcessChoice]]] = None,
) -> tuple[Optional[WechatProcessChoice], str]:
    """发现所有已启动且 hook 正在监听的微信；唯一账号自动复用，多账号显式选择。

    旧逻辑只看 ``default_port`` 是否由多个 PID 共享；一旦某个账号已经热迁移到 30002，
    30001/30002 各只有一个监听者，选择器反而永远不出现。这里按进程逐一发现 30001..30016
    的 hook 监听端口，因此共享端口和独立端口两种多开形态都进入同一个选择流程。
    """
    if pids_fn is None or port_fn is None:
        from widget.wechat.ports import listening_port_for_pid, wechat_pids
        pids_fn = pids_fn or wechat_pids
        port_fn = port_fn or listening_port_for_pid

    ports: dict[int, int] = {}
    for raw_pid in pids_fn() or []:
        pid = int(raw_pid or 0)
        if not pid:
            continue
        port = int(port_fn(pid) or 0)
        if port:
            ports[pid] = port
    if not ports:
        return None, ""

    discovered = (choices_fn or discover_wechat_choices)(sorted(ports))
    choices = [replace(choice, hook_port=ports.get(int(choice.pid), int(default_port or 0)))
               for choice in discovered if int(choice.pid) in ports]
    if not choices:
        return None, ""
    if len(choices) == 1:
        selected = choices[0]
        if not selected.wxid:
            return None, "无法识别唯一已登录微信的账号目录，为防止接错账号已取消接管"
        return selected, ""
    selected = chooser(choices)
    if selected is None:
        return None, "用户取消了个人微信接管选择"
    if int(selected.pid) not in ports:
        return None, "所选微信进程已变化，请重新选择"
    if not selected.wxid:
        return None, "无法识别所选微信的账号目录，为防止接错账号已取消接管"
    return replace(selected, hook_port=ports[int(selected.pid)]), ""


def _spawn_wechat(exe: str) -> int:
    import subprocess

    return int(subprocess.Popen([exe], cwd=str(Path(exe).parent or ".")).pid)


def ensure_selected_wechat_routable(
    selected: WechatProcessChoice,
    port: int,
    *,
    probe_fn=None,
    stop_fn=None,
    exe_fn=None,
    spawn_fn=None,
    owners_fn=None,
    choices_fn=None,
    sleep_fn=None,
    attempts: int = 60,
) -> tuple[Optional[WechatProcessChoice], str]:
    """兼容诊断辅助：重开所选账号以尝试改变共享端口分流，不结束未选微信。

    正常软件启动不调用本函数。它只供显式诊断场景使用：若所选 PID 已可达则零改动；否则退出并
    重新打开所选微信，再以权威 ``xwechat_files`` 目录确认新 PID 仍属于同一 wxid。
    """
    from widget.wechat.pid_routing import pid_accepts_connections
    from widget.wechat.ports import listening_pids_for_port

    probe = probe_fn or pid_accepts_connections
    port = int(port)
    if probe(int(selected.pid), port):
        return selected, ""

    try:
        exe = str((exe_fn or (lambda pid: psutil.Process(int(pid)).exe()))(selected.pid) or "")
    except Exception as exc:  # noqa: BLE001
        return None, f"无法取得所选微信程序路径，未调整任何其他微信（{exc}）"
    if not exe:
        return None, "无法取得所选微信程序路径，未调整任何其他微信"

    stopper = stop_fn or stop_wechat_process
    if not stopper(int(selected.pid)):
        return None, "所选微信当前不可达且无法重新打开；未选中的微信没有被关闭"
    try:
        (spawn_fn or _spawn_wechat)(exe)
    except Exception as exc:  # noqa: BLE001
        return None, f"所选微信重新打开失败（{exc}）；未选中的微信没有被关闭"

    owners = owners_fn or listening_pids_for_port
    discover = choices_fn or discover_wechat_choices
    nap = sleep_fn or time.sleep
    for _ in range(max(1, int(attempts))):
        nap(0.25)
        try:
            current = list(owners(port) or [])
            choices = list(discover(current) or [])
        except Exception:  # noqa: BLE001 - 微信/端口启动存在瞬态，继续等
            continue
        replacement = next((choice for choice in choices
                            if choice.wxid == selected.wxid and choice.pid != selected.pid), None)
        if replacement is not None and probe(int(replacement.pid), port):
            return replacement, ""
    return None, (
        "所选微信已尝试重新打开，但账号或 hook 尚未就绪；请确认该账号仍已登录后重新启动 AI客服。"
        "未选中的微信没有被关闭"
    )
