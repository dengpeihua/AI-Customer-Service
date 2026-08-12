"""修复 WeChat-Hook(4.1.10.27_new / version_new.dll 592KB) 收消息不可用的根因。

根因：hook 源码里 `g_IsLogin` 只初始化 0、无任何置 1 逻辑（登录检测 hook 被 main 分支砍），
而 db_mgr 用 `if(!g_IsLogin)` 挡死所有 DB 操作 → QueryDB 恒返回 "get database handle failed"。
修法：运行时把进程内的 `g_IsLogin` patch 成 1；之后 searchDatabases 会动态扫描拿到句柄。

难点已固化为常量：
- g_IsLogin 在 version_new.dll 的 RVA = 0x90ca8（用当前加载的 DLL 定位；旧版 472KB 是 0x736a0）。
- hook DLL 被 hideself 从 PEB 模块列表隐藏，须扫描进程内存按 SizeOfImage=0x99000 找真实基址。
- VirtualProtectEx + WriteProcessMemory，同用户权限即可（免管理员）。

用法：
    python -m widget.hook_patch          # 打一次
    python -m widget.hook_patch --watch  # 常驻，每 10s 确保 g_IsLogin=1（应对微信重启）
挂件集成：WeChatHookAdapter.start() 里调用 ensure_login_patched()。
"""
from __future__ import annotations
import ctypes
import json
import struct
import sys
import time
import urllib.request
from ctypes import wintypes

import psutil

from widget.hook_io import lock_for_port

G_ISLOGIN_RVA = 0x90CA8       # version_new.dll(592KB) 的 g_IsLogin
HOOK_SIZEOFIMAGE = 0x99000    # 该 DLL 的 SizeOfImage，用于内存里认出被隐藏的镜像
SAFE_SCANNER_SIZEOFIMAGE = 0x135000
SAFE_SCANNER_PE_TIMESTAMP = 0x9CC68B2C
_KNOWN_HOOK_IMAGE_SIZES = {HOOK_SIZEOFIMAGE, SAFE_SCANNER_SIZEOFIMAGE}
HOOK_PORT = 30001
MIN_WECHAT_PROCESS_AGE_SECONDS = 20.0

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.VirtualProtectEx.restype = wintypes.BOOL
_k32.VirtualProtectEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID, ctypes.c_size_t,
                                  wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
_k32.WriteProcessMemory.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.LPCVOID,
                                    ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
_k32.ReadProcessMemory.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.LPVOID,
                                   ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]


class _MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_ulonglong), ("AllocationBase", ctypes.c_ulonglong),
                ("AllocationProtect", ctypes.c_ulong), ("PartitionId", ctypes.c_ushort),
                ("_p0", ctypes.c_ushort), ("RegionSize", ctypes.c_ulonglong),
                ("State", ctypes.c_ulong), ("Protect", ctypes.c_ulong),
                ("Type", ctypes.c_ulong), ("_p1", ctypes.c_ulong)]


_k32.VirtualQueryEx.argtypes = [wintypes.HANDLE, wintypes.LPCVOID,
                                ctypes.POINTER(_MBI), ctypes.c_size_t]


def _hook_pid(port: int = HOOK_PORT) -> int | None:
    from widget.wechat.ports import listening_pids_for_port
    owners = listening_pids_for_port(port)
    return owners[0] if len(owners) == 1 else None


def _hook_port_conflict(port: int = HOOK_PORT) -> list[int]:
    """返回同一 hook 端口的重复监听者；0/1 个时为空。"""
    from widget.wechat.ports import listening_pids_for_port
    owners = listening_pids_for_port(port)
    return owners if len(owners) > 1 else []


def _read(h, addr, size=8):
    buf = (ctypes.c_ubyte * size)()
    n = ctypes.c_size_t()
    if _k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size, ctypes.byref(n)):
        return bytes(buf)
    return None


def _hook_image_identity(h, base: int) -> tuple[int, int] | None:
    hd = _read(h, base, 0x200)
    if not hd or len(hd) < 0x100 or hd[:2] != b"MZ":
        return None
    e = struct.unpack_from("<I", hd, 0x3C)[0]
    if not 0 < e < 0x180 or hd[e:e + 4] != b"PE\x00\x00":
        return None
    return (
        struct.unpack_from("<I", hd, e + 8)[0],
        struct.unpack_from("<I", hd, e + 0x50)[0],
    )


def _find_hook_bases(h) -> list[int]:
    """扫描 MEM_IMAGE 区域，按 SizeOfImage 认出被 hideself 隐藏的 hook DLL。
    返回**所有**匹配的基址——多于一个即基址不唯一，调用方应放弃写入（见 ensure_login_patched）。"""
    bases: list[int] = []
    addr = 0
    while addr < 0x7FFFFFFF0000:
        mbi = _MBI()
        if not _k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            break
        if mbi.State == 0x1000 and mbi.Type == 0x1000000 and mbi.BaseAddress == mbi.AllocationBase:
            identity = _hook_image_identity(h, mbi.AllocationBase)
            if identity and identity[1] in _KNOWN_HOOK_IMAGE_SIZES:
                bases.append(mbi.AllocationBase)
        addr = mbi.BaseAddress + (mbi.RegionSize or 0x1000)
    return bases


def _find_hook_base(h) -> int | None:
    """唯一匹配时返回该基址；0 个或多个匹配都返回 None（宁可不补，也绝不往有歧义的地址写）。"""
    bases = _find_hook_bases(h)
    return bases[0] if len(bases) == 1 else None


def _querydb_count(port: int = HOOK_PORT) -> int:
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/QueryDB/GetAllDBName",
                                     data=b"{}", headers={"Content-Type": "application/json"})
        with lock_for_port(port):
            return len(json.loads(urllib.request.urlopen(req, timeout=6).read()))
    except Exception:
        return -1


def _get_self_profile(port: int = HOOK_PORT) -> dict | None:
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/GetSelfProfile",
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        with lock_for_port(port):
            profile = json.loads(urllib.request.urlopen(req, timeout=4).read())
        return profile if isinstance(profile, dict) else None
    except Exception:
        return None


def _profile_indicates_logged_in(profile: object) -> bool:
    if not isinstance(profile, dict):
        return False
    return bool(str(profile.get("wxid", "")).strip()) and bool(
        str(profile.get("nickname", "")).strip()
    )


def _login_ready_for_patch(pid: int, port: int = HOOK_PORT) -> tuple[bool, str]:
    try:
        process_age = time.time() - psutil.Process(pid).create_time()
    except (psutil.Error, OSError):
        return False, "无法确认微信启动时间，暂不补丁"
    if process_age < MIN_WECHAT_PROCESS_AGE_SECONDS:
        return False, "微信仍在启动，暂不补丁"
    # M4：探活必须走**本实例自己的端口**——多实例下拿 A 号的就绪去批准写 B 号的内存，
    # 正是「写崩微信」最危险的一条路径。
    if not _profile_indicates_logged_in(_get_self_profile(port)):
        return False, "微信尚未完成登录，暂不补丁"
    return True, ""


def _classify_g_islogin(cur: int) -> str:
    """据读到的 g_IsLogin 当前值决定动作。g_IsLogin 合法值**只有 0 或 1**；
    读出别的值 = 基址扫错了（扫到别的模块/错误地址），**绝不能往那儿写**，否则会把微信写崩、
    进而触发微信『上次异常退出，是否修复』弹窗（客户绝不能看到）。
    返回：'already'(已=1，幂等跳过) / 'write'(=0，可安全置 1) / 'abort'(非 0/1，放弃本次)。"""
    if cur == 1:
        return "already"
    if cur == 0:
        return "write"
    return "abort"


def _pid_port_mismatch(pid: int, port: int) -> str:
    """pid 与 port 必须是**同一个进程**，否则返回一行拒绝理由（空串=配对成立）。

    就绪闸（_login_ready_for_patch）是拿 `port` 上的身份批准的，而写内存打在 `pid` 上：
    配对不成立就等于用 A 号的登录状态批准写 B 号的内存——正是「写崩微信」最危险的一条路径。
    查不到监听者时无法自证配对 → 同样拒绝（宁可这轮不补，下轮再试）。
    """
    conflict = _hook_port_conflict(port)
    if conflict:
        return (f"端口 {port} 被多个微信进程共同监听（pid={','.join(map(str, conflict))}）"
                " → 无法证明端口归属，放弃写入")
    try:
        listener = _hook_pid(port)
    except Exception as e:                     # noqa: BLE001 —— 查不到就当作无法自证
        return f"无法确认端口 {port} 的监听进程（{e}）→ 放弃写入，不冒险写崩微信"
    if listener is None:
        return f"端口 {port} 上已无监听进程 → 放弃写入，不冒险写崩微信"
    if listener != pid:
        return (f"端口/进程不匹配：{port} 的监听者 pid={listener} ≠ 目标 pid={pid}"
                f" → 放弃写入，不冒险写崩微信")
    return ""


def _patch_login_memory(pid: int, port: int, verify_querydb: bool = False) -> str:
    """只做已通过外层身份/端口闸后的内存读写；调用方必须先完成安全验证。"""
    h = _k32.OpenProcess(0x1F0FFF, False, pid)
    if not h:
        return f"OpenProcess 失败 err={ctypes.get_last_error()}"
    try:
        bases = _find_hook_bases(h)
        if not bases:
            return "未找到 hook DLL 镜像（SizeOfImage 不匹配，DLL 版本变了？）"
        if len(bases) > 1:
            return f"检测到 {len(bases)} 个同尺寸镜像，基址不唯一 → 放弃写入，不冒险写崩微信"
        if _hook_image_identity(h, bases[0]) == (
                SAFE_SCANNER_PE_TIMESTAMP, SAFE_SCANNER_SIZEOFIMAGE):
            suffix = f" QueryDB={_querydb_count(port)}库" if verify_querydb else ""
            return f"g_IsLogin 已=1（安全扫描器内置）{suffix}"
        addr = bases[0] + G_ISLOGIN_RVA
        raw = _read(h, addr)
        if raw is None:
            return "读 g_IsLogin 失败（地址不可读）—— 放弃本次，不写"
        cur = struct.unpack("<Q", raw)[0]
        action = _classify_g_islogin(cur)
        if action == "already":
            suffix = f" QueryDB={_querydb_count(port)}库" if verify_querydb else ""
            return f"g_IsLogin 已=1（幂等）{suffix}"
        if action == "abort":
            return f"基址校验失败：g_IsLogin 读出=0x{cur:x}（非 0/1），放弃写入，不冒险写崩微信"
        old = wintypes.DWORD()
        n = ctypes.c_size_t()
        _k32.VirtualProtectEx(h, ctypes.c_void_p(addr), 8, 0x04, ctypes.byref(old))
        ok = _k32.WriteProcessMemory(h, ctypes.c_void_p(addr), ctypes.byref(ctypes.c_uint64(1)),
                                     8, ctypes.byref(n))
        _k32.VirtualProtectEx(h, ctypes.c_void_p(addr), 8, old.value, ctypes.byref(old))
        after_raw = _read(h, addr)
        if after_raw is None:
            return "写入后无法复读 g_IsLogin，停止继续操作"
        after = struct.unpack("<Q", after_raw)[0]
        suffix = f" QueryDB={_querydb_count(port)}库" if verify_querydb else ""
        return f"patch g_IsLogin 0->{after}(write={bool(ok)}){suffix}"
    finally:
        _k32.CloseHandle(h)


def _ensure_login_patched_locked(pid: int | None = None, port: int = HOOK_PORT,
                                 verify_querydb: bool = False) -> str:
    """确保指定微信进程的 g_IsLogin=1。返回一行状态说明。幂等、且**永不写崩微信**。

    M4：pid/port 可显式指定（多实例，每号一个端口）。都不给时退化为 legacy 行为
    ——扫 30001 的监听者，与 M4 之前逐字节一致。
    """
    explicit_pid = bool(pid)
    pid = pid or _hook_pid(port)
    if not pid:
        conflict = _hook_port_conflict(port)
        if conflict:
            return (f"端口 {port} 被多个微信进程同时监听（pid={','.join(map(str, conflict))}）"
                    "，请求会随机串到不同账号 → 拒绝补丁；请关闭多余微信或为每个实例分配独立端口")
        return f"微信/hook 服务未运行（{port} 无监听）"
    login_ready, message = _login_ready_for_patch(pid, port)
    if not login_ready:
        return message
    if explicit_pid:
        # pid 是调用方给的：写内存前先自证它就是 `port` 的监听者（自己扫出来的 pid 天然成立）。
        mismatch = _pid_port_mismatch(pid, port)
        if mismatch:
            return mismatch
    return _patch_login_memory(pid, port, verify_querydb)


def ensure_login_patched(pid: int | None = None, port: int = HOOK_PORT,
                         verify_querydb: bool = False) -> str:
    """Patch one hook instance without racing its native HTTP/DB operations."""
    with lock_for_port(port):
        return _ensure_login_patched_locked(pid, port, verify_querydb)


def ensure_login_patched_routed(
    pid: int,
    port: int,
    wxid: str,
    *,
    verify_querydb: bool = False,
    process_age_fn=None,
    account_owner_fn=None,
    port_for_pid=None,
    owners_for_port=None,
    patch_memory_fn=None,
) -> str:
    """共享端口下只补所选 PID；以进程数据目录和 OS 监听表替代不唯一的端口身份探针。"""
    from widget.wechat.manager import process_owns_account
    from widget.wechat.ports import listening_pids_for_port, listening_port_for_pid

    pid, port, wxid = int(pid or 0), int(port or 0), str(wxid or "").strip()
    if not pid or not port or not wxid:
        return "共享端口补丁参数不完整，放弃写入"
    try:
        age = float((process_age_fn or (
            lambda value: time.time() - psutil.Process(value).create_time()
        ))(pid))
    except Exception:
        return "无法确认微信启动时间，暂不补丁"
    if age < MIN_WECHAT_PROCESS_AGE_SECONDS:
        return "微信仍在启动，暂不补丁"
    try:
        owns_account = bool((account_owner_fn or process_owns_account)(pid, wxid))
    except Exception:
        owns_account = False
    if not owns_account:
        return f"pid={pid} 的账号身份不匹配 {wxid}，放弃写入"
    try:
        actual_port = int((port_for_pid or listening_port_for_pid)(pid) or 0)
        owners = {int(value) for value in (owners_for_port or listening_pids_for_port)(port)}
    except Exception:
        return f"无法确认 pid={pid} 的端口归属，放弃写入"
    if actual_port != port or pid not in owners:
        return f"pid={pid} 已不再监听端口 {port}，放弃写入"
    with lock_for_port(port):
        return (patch_memory_fn or _patch_login_memory)(pid, port, verify_querydb)


def main() -> None:
    if "--watch" in sys.argv:
        print("[hook_patch watch] 每 10s 确保 g_IsLogin=1（应对微信重启）...")
        while True:
            print(time.strftime("%H:%M:%S"), ensure_login_patched())
            time.sleep(10)
    else:
        # The launcher invokes this before the widget starts, so this one DB
        # readiness probe cannot race the normal poller. Periodic in-process
        # repatching intentionally skips QueryDB.
        print(ensure_login_patched(verify_querydb=True))


if __name__ == "__main__":
    main()
