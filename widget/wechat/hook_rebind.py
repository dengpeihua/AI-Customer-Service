"""Move an already-running WeChat hook from a starved shared port.

The bundled hook starts its HTTP server once and keeps both the ``HttpServer``
object and the listening socket in stable locations.  For the one supported DLL
build we can close only that socket inside the selected WeChat process, wait for
the detached listener thread to finish, and call ``HttpServer::Start`` again on
an unused port.  The WeChat process and its login session stay alive.

Every native address below is guarded by the exact PE timestamp, image size and
the machine-code signature at the private ``Start`` entry.  An unknown DLL is
never modified.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import struct
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import psutil


class HookRebindError(RuntimeError):
    """The selected hook could not be migrated without risking the process."""


@dataclass(frozen=True)
class HookLayout:
    asset_sha256: str
    pe_timestamp: int
    image_size: int
    http_server_global_rva: int
    start_rva: int
    start_signature: bytes
    running_offset: int = 0x10
    server_ptr_offset: int = 0x18
    server_socket_offset: int = 0x48


SUPPORTED_LAYOUT = HookLayout(
    asset_sha256="23948e7d933ff40735ddc0cc516641fd9f935d67ffdec476bb8db61b79940f29",
    pe_timestamp=1782356974,
    image_size=0x99000,
    http_server_global_rva=0x90E08,
    start_rva=0x27440,
    start_signature=bytes.fromhex(
        "48895c241848896c2420565741564883ec70418bf0488bea488bd94533f64489"
    ),
)


def _asset_path() -> Path:
    return Path(__file__).resolve().parents[2] / "assets" / "hook" / "version_hook.dll"


def _verify_asset(layout: HookLayout = SUPPORTED_LAYOUT) -> None:
    try:
        digest = hashlib.sha256(_asset_path().read_bytes()).hexdigest()
    except OSError as exc:
        raise HookRebindError(f"无法读取 hook DLL：{exc}") from exc
    if digest != layout.asset_sha256:
        raise HookRebindError("hook DLL 指纹已变化，已停止热迁移以保护微信进程")


def ensure_unique_hook_port(
    pid: int,
    current_port: int,
    *,
    probe_fn=None,
    owners_fn=None,
    allocate_fn=None,
    rebind_fn=None,
    sleep_fn=None,
    verify_attempts: int = 20,
) -> int:
    """Return a safe port for ``pid``, hot-migrating only a starved shared port."""
    from widget.wechat.pid_routing import pid_accepts_connections
    from widget.wechat.ports import (
        PORT_BASE,
        PORT_MAX,
        allocate_port,
        listening_pids_for_port,
    )

    pid, current_port = int(pid), int(current_port)
    probe = probe_fn or (lambda target, port: pid_accepts_connections(
        target, port, attempts=24,
    ))
    owners = owners_fn or listening_pids_for_port
    allocate = allocate_fn or allocate_port
    migrate = rebind_fn or rebind_hook_port
    nap = sleep_fn or time.sleep

    if probe(pid, current_port):
        return current_port

    current_owners = sorted({int(value) for value in owners(current_port)})
    if pid not in current_owners:
        raise HookRebindError(
            f"端口 {current_port} 的监听者不含所选微信 pid={pid}，拒绝修改任何进程"
        )
    # A sole listener may simply still be starting.  The existing patch retry
    # path should handle that; hot migration is only for proven shared-port
    # starvation.
    if len(current_owners) == 1:
        return current_port

    new_port = allocate(lo=PORT_BASE + 1, hi=PORT_MAX, skip={current_port})
    if not new_port:
        raise HookRebindError(f"{PORT_BASE + 1}-{PORT_MAX} 没有可用的独立 hook 端口")
    new_port = int(new_port)
    if owners(new_port):
        raise HookRebindError(f"候选端口 {new_port} 已被占用，拒绝热迁移")

    migrate(pid, current_port, new_port)
    for _ in range(max(1, int(verify_attempts))):
        if probe(pid, new_port):
            return new_port
        nap(0.1)
    raise HookRebindError(
        f"已请求 pid={pid} 迁移到 {new_port}，但未验证到新监听；本次不连接其他微信"
    )


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.VirtualAllocEx.restype = wintypes.LPVOID
_k32.VirtualAllocEx.argtypes = [
    wintypes.HANDLE, wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD,
]
_k32.VirtualFreeEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD]
_k32.WriteProcessMemory.argtypes = [
    wintypes.HANDLE, wintypes.LPVOID, wintypes.LPCVOID, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]
_k32.CreateRemoteThread.restype = wintypes.HANDLE
_k32.CreateRemoteThread.argtypes = [
    wintypes.HANDLE, wintypes.LPVOID, ctypes.c_size_t, wintypes.LPVOID,
    wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
]
_k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_k32.GetExitCodeThread.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_k32.LoadLibraryW.restype = wintypes.HMODULE
_k32.LoadLibraryW.argtypes = [wintypes.LPCWSTR]
_k32.GetProcAddress.restype = ctypes.c_void_p
_k32.GetProcAddress.argtypes = [wintypes.HMODULE, ctypes.c_char_p]
_k32.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]


class _MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("PartitionId", wintypes.WORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


_k32.VirtualQuery.argtypes = [
    wintypes.LPCVOID, ctypes.POINTER(_MEMORY_BASIC_INFORMATION), ctypes.c_size_t,
]


class _MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD),
        ("modBaseAddr", ctypes.POINTER(ctypes.c_ubyte)),
        ("modBaseSize", wintypes.DWORD),
        ("hModule", wintypes.HMODULE),
        ("szModule", wintypes.WCHAR * 256),
        ("szExePath", wintypes.WCHAR * 260),
    ]


_k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_k32.Module32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MODULEENTRY32W)]
_k32.Module32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MODULEENTRY32W)]


def _target_modules(pid: int) -> dict[str, int]:
    snapshot = _k32.CreateToolhelp32Snapshot(0x00000008 | 0x00000010, int(pid))
    if snapshot == wintypes.HANDLE(-1).value:
        raise HookRebindError(f"无法枚举 pid={pid} 的系统模块，err={ctypes.get_last_error()}")
    modules: dict[str, int] = {}
    try:
        entry = _MODULEENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = _k32.Module32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            modules[str(entry.szModule).lower()] = ctypes.cast(
                entry.modBaseAddr, ctypes.c_void_p,
            ).value
            ok = _k32.Module32NextW(snapshot, ctypes.byref(entry))
    finally:
        _k32.CloseHandle(snapshot)
    return modules


def _remote_proc(modules: dict[str, int], module_name: str, proc_name: bytes) -> int:
    local_module = _k32.LoadLibraryW(module_name)
    local_proc = int(_k32.GetProcAddress(local_module, proc_name) or 0)
    if not local_proc:
        raise HookRebindError(f"无法解析 {module_name}!{proc_name.decode('ascii')}")

    mbi = _MEMORY_BASIC_INFORMATION()
    if not _k32.VirtualQuery(
        ctypes.c_void_p(local_proc), ctypes.byref(mbi), ctypes.sizeof(mbi),
    ):
        raise HookRebindError("无法解析本机系统函数所属模块")
    local_base = int(mbi.AllocationBase or 0)
    name_buf = ctypes.create_unicode_buffer(260)
    if not _k32.GetModuleFileNameW(wintypes.HMODULE(local_base), name_buf, len(name_buf)):
        raise HookRebindError("无法读取本机系统函数模块名")
    actual_name = os.path.basename(name_buf.value).lower()
    remote_base = int(modules.get(actual_name) or 0)
    if not remote_base:
        raise HookRebindError(f"目标微信未加载系统模块 {actual_name}")
    return remote_base + (local_proc - local_base)


def _read_exact(handle, address: int, size: int) -> bytes:
    from widget.hook_patch import _read

    data = _read(handle, int(address), int(size))
    if data is None or len(data) != size:
        raise HookRebindError(f"无法读取目标微信内存 0x{address:x}")
    return data


def _validated_hook_base(handle, layout: HookLayout = SUPPORTED_LAYOUT) -> int:
    from widget.hook_patch import _find_hook_bases

    bases = list(_find_hook_bases(handle))
    if len(bases) != 1:
        raise HookRebindError(f"hook DLL 内存镜像数量为 {len(bases)}，无法唯一定位")
    base = int(bases[0])
    header = _read_exact(handle, base, 0x200)
    pe_offset = struct.unpack_from("<I", header, 0x3C)[0]
    if header[:2] != b"MZ" or header[pe_offset:pe_offset + 4] != b"PE\0\0":
        raise HookRebindError("目标 hook 的 PE 头无效")
    timestamp = struct.unpack_from("<I", header, pe_offset + 8)[0]
    image_size = struct.unpack_from("<I", header, pe_offset + 0x50)[0]
    signature = _read_exact(handle, base + layout.start_rva, len(layout.start_signature))
    if (timestamp, image_size, signature) != (
        layout.pe_timestamp, layout.image_size, layout.start_signature,
    ):
        raise HookRebindError("目标 hook 版本或代码签名不匹配，拒绝热迁移")
    return base


def _close_payload(http_server: int, shutdown_addr: int, closesocket_addr: int,
                   layout: HookLayout = SUPPORTED_LAYOUT) -> bytes:
    code = bytearray(b"\x53\x48\x83\xec\x30")  # preserve RBX + shadow/local space
    code += b"\x48\xbb" + struct.pack("<Q", http_server)
    code += b"\x48\x8b\x43" + bytes([layout.server_ptr_offset])
    code += b"\x48\x85\xc0\x0f\x84\x00\x00\x00\x00"
    missing_server_rel = len(code) - 4
    code += b"\x48\xc7\xc1\xff\xff\xff\xff"
    code += b"\x48\x87\x48" + bytes([layout.server_socket_offset])
    code += b"\x48\x83\xf9\xff\x74\x00"
    closed_socket_rel = len(code) - 1
    code += b"\x48\x89\x4c\x24\x20\xba\x02\x00\x00\x00"
    code += b"\x48\xb8" + struct.pack("<Q", shutdown_addr) + b"\xff\xd0"
    code += b"\x48\x8b\x4c\x24\x20"
    code += b"\x48\xb8" + struct.pack("<Q", closesocket_addr) + b"\xff\xd0"
    stop_at = len(code)
    code += b"\xc6\x43" + bytes([layout.running_offset]) + b"\x00\x31\xc0\xeb\x05"
    fail_at = len(code)
    code += b"\xb8\x01\x00\x00\xe0"
    code[missing_server_rel:missing_server_rel + 4] = struct.pack(
        "<i", fail_at - (missing_server_rel + 4),
    )
    code[closed_socket_rel] = (stop_at - (closed_socket_rel + 1)) & 0xFF
    code += b"\x48\x83\xc4\x30\x5b\xc3"
    return bytes(code)


def _start_payload(http_server: int, host_string: int, port: int, start_addr: int) -> bytes:
    code = bytearray(b"\x53\x48\x83\xec\x20")
    code += b"\x48\xbb" + struct.pack("<Q", http_server)
    code += b"\x48\x89\xd9"
    code += b"\x48\xba" + struct.pack("<Q", host_string)
    code += b"\x41\xb8" + struct.pack("<I", int(port))
    code += b"\x48\xb8" + struct.pack("<Q", start_addr) + b"\xff\xd0"
    code += b"\x0f\xb6\xc0\x48\x83\xc4\x20\x5b\xc3"
    return bytes(code)


def _loopback_host_string() -> bytes:
    """MSVC ``std::string`` (small-string form) for the local-only bind host."""
    inline = b"127.0.0.1\0" + (b"\0" * 6)
    return inline + struct.pack("<QQ", 9, 15)


def _run_remote(handle, payload: bytes, *, extra: bytes = b"", timeout_ms: int = 5000) -> int:
    size = len(payload) + len(extra)
    remote = _k32.VirtualAllocEx(handle, None, size, 0x1000 | 0x2000, 0x40)
    if not remote:
        raise HookRebindError(f"VirtualAllocEx 失败，err={ctypes.get_last_error()}")
    try:
        blob = payload + extra
        source = ctypes.create_string_buffer(blob)
        written = ctypes.c_size_t()
        if not _k32.WriteProcessMemory(
            handle, remote, source, len(blob), ctypes.byref(written),
        ) or written.value != len(blob):
            raise HookRebindError(f"WriteProcessMemory 失败，err={ctypes.get_last_error()}")
        thread_id = wintypes.DWORD()
        thread = _k32.CreateRemoteThread(
            handle, None, 0, remote, None, 0, ctypes.byref(thread_id),
        )
        if not thread:
            raise HookRebindError(f"CreateRemoteThread 失败，err={ctypes.get_last_error()}")
        try:
            wait = _k32.WaitForSingleObject(thread, int(timeout_ms))
            if wait != 0:
                raise HookRebindError(f"hook 端口迁移线程未正常结束，wait=0x{wait:x}")
            exit_code = wintypes.DWORD()
            if not _k32.GetExitCodeThread(thread, ctypes.byref(exit_code)):
                raise HookRebindError("无法读取 hook 端口迁移结果")
            return int(exit_code.value)
        finally:
            _k32.CloseHandle(thread)
    finally:
        _k32.VirtualFreeEx(handle, remote, 0, 0x8000)


def rebind_hook_port(pid: int, old_port: int, new_port: int,
                     *, layout: HookLayout = SUPPORTED_LAYOUT) -> None:
    """Rebind the supported hook DLL in ``pid`` without terminating WeChat."""
    from widget.wechat.ports import listening_pids_for_port

    pid, old_port, new_port = int(pid), int(old_port), int(new_port)
    try:
        process = psutil.Process(pid)
        if process.name().lower() != "weixin.exe" or not process.is_running():
            raise HookRebindError(f"pid={pid} 不是正在运行的 Weixin.exe")
    except psutil.Error as exc:
        raise HookRebindError(f"无法确认所选微信 pid={pid}：{exc}") from exc
    if pid not in listening_pids_for_port(old_port):
        raise HookRebindError(f"pid={pid} 已不再监听原端口 {old_port}")
    if listening_pids_for_port(new_port):
        raise HookRebindError(f"新端口 {new_port} 已被占用")

    _verify_asset(layout)
    access = 0x0002 | 0x0008 | 0x0010 | 0x0020 | 0x0400 | 0x100000
    handle = _k32.OpenProcess(access, False, pid)
    if not handle:
        raise HookRebindError(
            f"无法打开所选微信 pid={pid}（err={ctypes.get_last_error()}），请从管理员启动入口运行"
        )
    try:
        base = _validated_hook_base(handle, layout)
        http_server = struct.unpack(
            "<Q", _read_exact(handle, base + layout.http_server_global_rva, 8),
        )[0]
        if http_server < 0x10000:
            raise HookRebindError("目标 hook 的 HttpServer 对象尚未初始化")
        server = struct.unpack(
            "<Q", _read_exact(handle, http_server + layout.server_ptr_offset, 8),
        )[0]
        running = _read_exact(handle, http_server + layout.running_offset, 1)[0]
        sock = struct.unpack(
            "<Q", _read_exact(handle, server + layout.server_socket_offset, 8),
        )[0] if server >= 0x10000 else 0xFFFFFFFFFFFFFFFF
        if not running or server < 0x10000 or sock in (0, 0xFFFFFFFFFFFFFFFF):
            raise HookRebindError("目标 hook 的监听对象状态不符合热迁移前置条件")

        modules = _target_modules(pid)
        shutdown_addr = _remote_proc(modules, "ws2_32.dll", b"shutdown")
        closesocket_addr = _remote_proc(modules, "ws2_32.dll", b"closesocket")
        close_code = _close_payload(http_server, shutdown_addr, closesocket_addr, layout)
        if _run_remote(handle, close_code) != 0:
            raise HookRebindError("关闭旧 hook 监听失败")

        # The original detached Run thread must finish before its Server object
        # is replaced by the new Run thread.
        time.sleep(0.25)
        host = _loopback_host_string()
        allocation_size = 128 + len(host)
        remote = _k32.VirtualAllocEx(handle, None, allocation_size, 0x3000, 0x40)
        if not remote:
            raise HookRebindError(f"分配新端口启动参数失败，err={ctypes.get_last_error()}")
        try:
            host_address = int(remote) + 96
            start_code = _start_payload(
                http_server, host_address, new_port, base + layout.start_rva,
            )
            blob = start_code + (b"\x90" * (96 - len(start_code))) + host
            source = ctypes.create_string_buffer(blob)
            written = ctypes.c_size_t()
            if not _k32.WriteProcessMemory(
                handle, remote, source, len(blob), ctypes.byref(written),
            ) or written.value != len(blob):
                raise HookRebindError("写入新端口启动参数失败")
            thread_id = wintypes.DWORD()
            thread = _k32.CreateRemoteThread(
                handle, None, 0, remote, None, 0, ctypes.byref(thread_id),
            )
            if not thread:
                raise HookRebindError(f"启动新 hook 监听失败，err={ctypes.get_last_error()}")
            try:
                wait = _k32.WaitForSingleObject(thread, 5000)
                exit_code = wintypes.DWORD()
                if wait != 0 or not _k32.GetExitCodeThread(thread, ctypes.byref(exit_code)) \
                        or exit_code.value != 1:
                    raise HookRebindError("hook 的 Start 调用未成功返回")
            finally:
                _k32.CloseHandle(thread)
        finally:
            _k32.VirtualFreeEx(handle, remote, 0, 0x8000)
    finally:
        _k32.CloseHandle(handle)
