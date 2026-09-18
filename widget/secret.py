"""登录密码加密存储：Windows DPAPI（CryptProtectData/UnprotectData）。

密文仅能在**同一台机器、同一 Windows 用户**下解开，写进 widget_config.yaml 的 `password_enc` 字段，
避免明文密码随配置文件被同步/备份/误读泄露。纯 ctypes 调 crypt32，无额外依赖。

给密码加密（隐藏输入；把结果填进 widget_config.yaml 的 password_enc，并删掉明文 password）：
    .venv\\Scripts\\python.exe -m widget.secret
"""
from __future__ import annotations

import base64
import ctypes
from getpass import getpass
import sys
from ctypes import wintypes

_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


_crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_crypt32.CryptProtectData.restype = wintypes.BOOL
_crypt32.CryptProtectData.argtypes = [ctypes.POINTER(_BLOB), wintypes.LPCWSTR,
    ctypes.POINTER(_BLOB), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_BLOB)]
_crypt32.CryptUnprotectData.restype = wintypes.BOOL
_crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(_BLOB), ctypes.c_void_p,
    ctypes.POINTER(_BLOB), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_BLOB)]


def _run(fn, data: bytes) -> bytes:
    buf_in = ctypes.create_string_buffer(data, len(data))
    blob_in = _BLOB(len(data), ctypes.cast(buf_in, ctypes.POINTER(ctypes.c_char)))
    blob_out = _BLOB()
    ok = fn(ctypes.byref(blob_in), None, None, None, None,
            _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    if not ok:
        raise OSError(f"DPAPI 调用失败 err={ctypes.get_last_error()}")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        _kernel32.LocalFree(blob_out.pbData)


def encrypt(plaintext: str) -> str:
    """明文 → DPAPI 密文（base64 字符串）。"""
    return base64.b64encode(_run(_crypt32.CryptProtectData, plaintext.encode("utf-8"))).decode("ascii")


def decrypt(enc_b64: str) -> str:
    """DPAPI 密文（base64）→ 明文。"""
    return _run(_crypt32.CryptUnprotectData, base64.b64decode(enc_b64)).decode("utf-8")


def resolve_password(cfg) -> str:
    """取实际登录密码：优先解密 password_enc；损坏/换机器则回落到明文 password。"""
    enc = getattr(cfg, "password_enc", "")
    if enc:
        try:
            return decrypt(enc)
        except Exception:
            pass       # 密文无效（换机器/损坏）→ 回落明文
    return getattr(cfg, "password", "")     # InstanceConfig 只有 password_enc，无 password 字段兜底


def main() -> None:
    if len(sys.argv) > 1:
        print("请勿把密码写在命令行中；直接运行 python -m widget.secret。", file=sys.stderr)
        raise SystemExit(2)
    plaintext = getpass("后端登录密码（隐藏输入）: ")
    if not plaintext:
        print("密码不能为空。", file=sys.stderr)
        raise SystemExit(2)
    enc = encrypt(plaintext)
    print("已加密。请在 widget_config.yaml 里这样填（并删除明文 password 行）：\n")
    print(f"password_enc: {enc}")


if __name__ == "__main__":
    main()
