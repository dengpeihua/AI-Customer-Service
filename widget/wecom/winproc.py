"""Windows 进程/版本小工具（连接器默认依赖；测试里被 fake 替换，故这里保持薄且防御性）。"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

TH32CS_SNAPPROCESS = 0x00000002
_MAX_PATH = 260


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_char * _MAX_PATH),
    ]


def find_process_pid(name: str) -> int | None:
    """按可执行名（不分大小写）找第一个匹配进程 pid，找不到返回 None。仅 Windows。"""
    try:
        k32 = ctypes.windll.kernel32
    except AttributeError:
        return None
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == wintypes.HANDLE(-1).value or snap == -1:
        return None
    try:
        entry = _PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32)
        target = name.lower().encode("mbcs", "ignore")
        if not k32.Process32First(snap, ctypes.byref(entry)):
            return None
        while True:
            if entry.szExeFile.lower() == target:
                return int(entry.th32ProcessID)
            if not k32.Process32Next(snap, ctypes.byref(entry)):
                return None
    finally:
        k32.CloseHandle(snap)


def find_process_pids(name: str) -> list[int]:
    """按可执行名（不分大小写）找【全部】匹配进程 pid，找不到返回 []。仅 Windows。"""
    try:
        k32 = ctypes.windll.kernel32
    except AttributeError:
        return []
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == wintypes.HANDLE(-1).value or snap == -1:
        return []
    pids: list[int] = []
    try:
        entry = _PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32)
        target = name.lower().encode("mbcs", "ignore")
        if not k32.Process32First(snap, ctypes.byref(entry)):
            return []
        while True:
            if entry.szExeFile.lower() == target:
                pids.append(int(entry.th32ProcessID))
            if not k32.Process32Next(snap, ctypes.byref(entry)):
                break
    finally:
        k32.CloseHandle(snap)
    return pids


def file_version(path: str) -> str:
    """读取 PE 文件版本号（如 '5.0.3.6005'）。读不到返回空串（绝不抛，交由上层版本闸决定）。仅 Windows。"""
    try:
        ver = ctypes.windll.version
    except (AttributeError, OSError):
        return ""
    try:
        size = ver.GetFileVersionInfoSizeW(ctypes.c_wchar_p(path), None)
        if not size:
            return ""
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(ctypes.c_wchar_p(path), 0, size, buf):
            return ""
        ffi = ctypes.c_void_p()
        length = wintypes.UINT()
        if not ver.VerQueryValueW(buf, ctypes.c_wchar_p("\\"),
                                  ctypes.byref(ffi), ctypes.byref(length)) or not length.value:
            return ""
        # VS_FIXEDFILEINFO: dwSignature, dwStrucVersion, dwFileVersionMS, dwFileVersionLS, ...
        data = ctypes.cast(ffi, ctypes.POINTER(wintypes.DWORD * 13)).contents
        ms, ls = data[2], data[3]
        return f"{ms >> 16 & 0xFFFF}.{ms & 0xFFFF}.{ls >> 16 & 0xFFFF}.{ls & 0xFFFF}"
    except Exception:
        return ""
