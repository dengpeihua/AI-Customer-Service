"""单实例锁（Windows 命名互斥量）：防止挂件多开。

多开的危害：多个轮询进程会对同一条抖音私信消息**各自回复一次**（重复回复），
且每个实例各有托盘图标，右键「退出」只退一个、其余仍在跑，看起来"退不掉"。

用法（在 run()/headless main() 最前面）：
    if not acquire("AI-customer-service-widget"):
        print("挂件已在运行，勿重复启动。"); return
互斥量句柄持有到进程退出即自动释放，无需手动清理。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

_ERROR_ALREADY_EXISTS = 183

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

_held: list = []   # 持有句柄，防止被 GC 提前释放互斥量


def acquire(name: str) -> bool:
    """尝试获取命名互斥量。True=本进程是唯一实例；False=已有实例在跑。"""
    h = _kernel32.CreateMutexW(None, True, name)
    err = ctypes.get_last_error()
    if not h:
        return True                         # 创建失败就不拦，别把用户挡在门外
    if err == _ERROR_ALREADY_EXISTS:
        _kernel32.CloseHandle(h)
        return False
    _held.append(h)                         # 持有到进程退出
    return True
