"""个人微信非文本消息解析、附件定位与图片预览解密。

这个模块只做只读工作：解析消息 XML、通过 hook 查询附件索引、读取本地附件。
它不会下载任意链接卡片资源，也不会发送、删除或改写微信数据。
"""
from __future__ import annotations

import os
import re
import struct
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from defusedxml import ElementTree as SafeET


_HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")
_SAFE_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def split_local_type(raw_type: int) -> tuple[int, int]:
    """把微信 64 位 local_type 拆成主类型（低 32 位）与 app 子类型（高 32 位）。"""
    value = _int(raw_type) & 0xFFFFFFFFFFFFFFFF
    return value & 0xFFFFFFFF, (value >> 32) & 0xFFFFFFFF


def _xml_root(content: str):
    text = str(content or "").strip().lstrip("\ufeff")
    if not text or "<" not in text:
        return None
    try:
        return SafeET.fromstring(text)
    except Exception:
        # 极少数群聊/转发记录会在 XML 前带一小段发送者前缀。
        start = text.find("<")
        try:
            return SafeET.fromstring(text[start:])
        except Exception:
            return None


def _first_text(root, *paths: str) -> str:
    if root is None:
        return ""
    for path in paths:
        node = root.find(path)
        if node is not None and node.text:
            value = node.text.strip()
            if value:
                return value
    return ""


def _first_attr(root, *names: str) -> str:
    if root is None:
        return ""
    for node in root.iter():
        for name in names:
            value = str(node.attrib.get(name, "") or "").strip()
            if value:
                return value
    return ""


def _safe_http_url(value: str) -> str:
    value = str(value or "").strip()
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    return value if parsed.scheme.lower() in {"http", "https"} and parsed.netloc else ""


def _friendly_size(size: int) -> str:
    value = max(0, _int(size))
    if not value:
        return ""
    if value < 1024:
        return f"{value} B"
    if value < 1024 ** 2:
        return f"{value / 1024:.1f} KB"
    if value < 1024 ** 3:
        return f"{value / 1024 ** 2:.1f} MB"
    return f"{value / 1024 ** 3:.1f} GB"


def parse_wechat_message(raw_type: int, content: str) -> dict:
    """把一行消息还原成可供 GUI 安全渲染的结构化字典。"""
    main_type, app_subtype = split_local_type(raw_type)
    root = _xml_root(content)
    base = {"raw_type": _int(raw_type), "main_type": main_type,
            "app_subtype": app_subtype, "kind": "unknown", "text": "[其他消息]"}

    if main_type == 1:
        return {**base, "kind": "text", "text": str(content or "")}
    if main_type == 3:
        md5 = _first_attr(root, "md5", "cdnmidimgurl", "cdnthumburl")
        return {**base, "kind": "image", "text": "[图片]", "md5": md5}
    if main_type == 34:
        duration = _int(_first_attr(root, "voicelength", "length"))
        return {**base, "kind": "voice", "text": "[语音消息]", "duration_ms": duration}
    if main_type == 43:
        duration = _int(_first_attr(root, "playlength", "length")) * 1000
        md5 = _first_attr(root, "md5", "cdnvideourl")
        return {**base, "kind": "video", "text": "[视频]", "duration_ms": duration, "md5": md5}
    if main_type == 47:
        return {**base, "kind": "emoji", "text": "[表情包]",
                "md5": _first_attr(root, "md5"),
                "remote_url": _safe_http_url(_first_attr(root, "cdnurl", "thumburl"))}
    if main_type == 10000:
        return {**base, "kind": "system", "text": str(content or "[系统消息]")}
    if main_type != 49:
        return base

    if not app_subtype:
        app_subtype = _int(_first_text(root, ".//appmsg/type", ".//type"))
        base["app_subtype"] = app_subtype

    title = _first_text(root, ".//appmsg/title", ".//title") or "应用消息"
    description = _first_text(root, ".//appmsg/des", ".//des", ".//summary")
    url = _safe_http_url(_first_text(
        root, ".//appmsg/url", ".//url", ".//weburl", ".//finderFeed/url"))
    thumb_url = _safe_http_url(_first_text(
        root, ".//appmsg/thumburl", ".//thumburl", ".//finderFeed/coverUrl"))

    if app_subtype == 57:
        refer = root.find(".//refermsg") if root is not None else None
        quote_sender = _first_text(refer, "./displayname", "./fromusr")
        quote_type = _int(_first_text(refer, "./type"))
        quote_text = _first_text(refer, "./content")
        if quote_text and split_local_type(quote_type)[0] != 1:
            nested = parse_wechat_message(quote_type, quote_text)
            quote_text = str(nested.get("title") or nested.get("text") or quote_text)
        return {
            **base,
            "kind": "quote",
            "text": title if title != "应用消息" else (description or "[引用回复]"),
            "title": title if title != "应用消息" else (description or "[引用回复]"),
            "quote_sender": quote_sender,
            "quote_text": quote_text or "[无法显示的引用消息]",
            "quote_type": quote_type,
        }

    if app_subtype == 6:
        file_ext = _first_text(root, ".//appattach/fileext")
        file_name = title
        if file_ext and not file_name.lower().endswith("." + file_ext.lower().lstrip(".")):
            file_name += "." + file_ext.lstrip(".")
        file_size = _int(_first_text(root, ".//appattach/totallen", ".//totallen"))
        size_label = _friendly_size(file_size)
        text = f"文件：{file_name}" + (f"（{size_label}）" if size_label else "")
        return {**base, "kind": "file", "text": text, "title": file_name,
                "description": description, "file_name": file_name, "file_size": file_size,
                "url": url, "md5": _first_text(root, ".//appattach/filemd5", ".//md5")}

    kind = {
        5: "link", 19: "forward", 33: "miniapp", 36: "miniapp",
        62: "app_post", 2000: "app_post",
    }.get(app_subtype, "app")
    labels = {
        "link": "链接", "forward": "转发的聊天记录", "miniapp": "小程序",
        "app_post": "应用帖子", "app": "应用消息",
    }
    return {**base, "kind": kind, "text": f"{labels[kind]}：{title}", "title": title,
            "description": description, "url": url, "thumb_url": thumb_url}


def is_standard_image(path: str | Path) -> bool:
    p = Path(path)
    return p.suffix.lower() in _SAFE_IMAGE_EXTENSIONS and p.is_file()


def decrypt_wechat_image(data: bytes, aes_key: bytes | None = None,
                         xor_key: int = 0x88) -> bytes:
    """解出微信图片缓存；支持旧版 XOR/V1 与 4.x V2（V2 需要当前进程图片密钥）。"""
    if not data:
        raise ValueError("图片数据为空")
    signatures = ((b"\xff\xd8\xff", "jpg"), (b"\x89PNG", "png"),
                  (b"GIF8", "gif"), (b"RIFF", "webp"))
    if any(data.startswith(sig) for sig, _ in signatures):
        return data
    if data.startswith((b"\x07\x08V2\x08\x07", b"\x07\x08V1\x08\x07")):
        is_v1 = data.startswith(b"\x07\x08V1\x08\x07")
        effective_key = b"cfcd208495d565ef" if is_v1 else aes_key
        if not effective_key or len(effective_key) != 16:
            raise ValueError("微信 V2 图片需要 16 字节预览密钥")
        if len(data) < 15:
            raise ValueError("微信 V2 图片头不完整")
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.padding import PKCS7
        aes_size, xor_size = struct.unpack_from("<II", data, 6)
        encrypted_size = (aes_size // 16 + 1) * 16
        start = 15
        if start + encrypted_size > len(data):
            raise ValueError("微信 V2 图片长度不合法")
        padded = Cipher(algorithms.AES(effective_key), modes.ECB()).decryptor().update(
            data[start:start + encrypted_size])
        unpadder = PKCS7(128).unpadder()
        prefix = unpadder.update(padded) + unpadder.finalize()
        raw_end = len(data) - min(max(0, xor_size), len(data) - start - encrypted_size)
        middle = data[start + encrypted_size:raw_end]
        tail = bytes(value ^ (xor_key & 0xFF) for value in data[raw_end:])
        decoded = prefix + middle + tail
        if not any(decoded.startswith(sig) for sig, _ in signatures):
            raise ValueError("图片预览密钥不匹配")
        return decoded

    # 3.x/早期 4.x 常见格式：首字节与图片签名异或即可推导整文件 key。
    for signature, _ in signatures:
        key = data[0] ^ signature[0]
        decoded = bytes(value ^ key for value in data)
        if decoded.startswith(signature):
            return decoded
    raise ValueError("不支持的微信图片缓存格式")


def configured_image_key() -> bytes | None:
    """从本机环境读取可选密钥；只返回字节，调用方不得记录其值。"""
    raw = str(os.getenv("WECHAT_IMAGE_AES_KEY", "") or "").strip()
    if not raw:
        return None
    if len(raw) == 32 and all(c in "0123456789abcdefABCDEF" for c in raw):
        return bytes.fromhex(raw)
    value = raw.encode("utf-8")
    return value if len(value) == 16 else None


def find_live_image_key(pid: int, image_path: str | Path, *, max_bytes: int = 1536 * 1024 ** 2,
                        max_seconds: float = 45.0) -> bytes | None:
    """只读扫描当前微信进程中短暂驻留的 V2 图片密钥，并用目标图片首块校验。

    微信只有在用户近期打开过图片时才可能让密钥驻留内存；找不到是正常结果。扫描有时间和
    字节上限，绝不把候选密钥写入日志或磁盘。
    """
    if os.name != "nt" or _int(pid) <= 0:
        return None
    data = Path(image_path).read_bytes()
    if not data.startswith(b"\x07\x08V2") or len(data) < 31:
        return None
    encrypted_first = data[15:31]
    try:
        import ctypes
        from ctypes import wintypes
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        class MemoryBasicInformation(ctypes.Structure):
            _fields_ = [
                ("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wintypes.DWORD), ("PartitionId", wintypes.WORD),
                ("RegionSize", ctypes.c_size_t), ("State", wintypes.DWORD),
                ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.VirtualQueryEx.restype = ctypes.c_size_t
        kernel32.ReadProcessMemory.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x0400 | 0x0010, False, _int(pid))
        if not handle:
            return None
        started, scanned, address = time.monotonic(), 0, 0
        key_pattern = re.compile(
            rb"(?<![0-9A-Za-z])(?:[0-9A-Za-z]{32}|[0-9A-Za-z]{16})(?![0-9A-Za-z])")
        try:
            mbi = MemoryBasicInformation()
            while scanned < max_bytes and time.monotonic() - started < max_seconds:
                got = kernel32.VirtualQueryEx(
                    handle, ctypes.c_void_p(address), ctypes.byref(mbi), ctypes.sizeof(mbi))
                if not got:
                    break
                base, region_size = int(mbi.BaseAddress or 0), int(mbi.RegionSize or 0)
                next_address = base + max(region_size, 0x1000)
                readable = (mbi.State == 0x1000 and mbi.Type == 0x20000 and
                            not (mbi.Protect & 0x100) and (mbi.Protect & 0xFF) not in {0, 1})
                if readable:
                    offset, overlap = 0, b""
                    while offset < region_size and scanned < max_bytes \
                            and time.monotonic() - started < max_seconds:
                        size = min(4 * 1024 ** 2, region_size - offset, max_bytes - scanned)
                        buffer = ctypes.create_string_buffer(size)
                        read = ctypes.c_size_t()
                        ok = kernel32.ReadProcessMemory(
                            handle, ctypes.c_void_p(base + offset), buffer, size, ctypes.byref(read))
                        chunk = overlap + buffer.raw[:read.value] if ok and read.value else b""
                        for match in key_pattern.finditer(chunk):
                            raw_candidate = match.group(0)
                            candidates = {raw_candidate[:16], raw_candidate[-16:]}
                            for candidate in candidates:
                                first = Cipher(
                                    algorithms.AES(candidate), modes.ECB()).decryptor().update(
                                        encrypted_first)
                                if first.startswith(
                                        (b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"RIFF")):
                                    return candidate
                        overlap = chunk[-33:] if chunk else b""
                        offset += size
                        scanned += size
                address = next_address
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None
    return None


def read_image_preview(path: str | Path, pid: int = 0, *, scan_live_key: bool = False) -> bytes:
    p = Path(path)
    data = p.read_bytes()
    xor_key = _int(os.getenv("WECHAT_IMAGE_XOR_KEY", "136"), 136)
    key = configured_image_key()
    if not key and scan_live_key and data.startswith(b"\x07\x08V2"):
        key = find_live_image_key(pid, p)
    return decrypt_wechat_image(data, key, xor_key)


class WechatMediaResolver:
    """把消息里的 md5/文件名映射到当前微信账号的本地附件。"""

    def __init__(self, query_db: Callable[[str, str], list[dict]], account_root: Path | None = None):
        self._query_db = query_db
        self.account_root = Path(account_root) if account_root else None
        self._dir_names: dict[int, str] | None = None
        self._hardlink_cache: dict[tuple[str, str], dict] = {}

    def set_account_root(self, account_root: Path | None) -> None:
        if account_root:
            self.account_root = Path(account_root)

    @staticmethod
    def _quote(value: str) -> str:
        return str(value).replace("'", "''")

    def _directories(self) -> dict[int, str]:
        if self._dir_names is None:
            try:
                rows = self._query_db("hardlink.db", "SELECT rowid AS rid, username FROM dir2id")
            except Exception:
                rows = []
            self._dir_names = {_int(row.get("rid")): str(row.get("username", "") or "")
                               for row in rows}
        return self._dir_names

    def _validated(self, *parts: str) -> str:
        if not self.account_root or not all(parts):
            return ""
        root = self.account_root.resolve()
        candidate = root.joinpath(*parts).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return ""
        return str(candidate) if candidate.is_file() else ""

    def _hardlink(self, table: str, where: str) -> dict:
        cache_key = (table, where)
        if cache_key in self._hardlink_cache:
            return self._hardlink_cache[cache_key]
        try:
            rows = self._query_db(
                "hardlink.db",
                f"SELECT file_name, dir1, dir2, file_size, type FROM {table} "
                f"WHERE {where} ORDER BY modify_time DESC LIMIT 1",
            )
        except Exception:
            rows = []
        result = rows[0] if rows else {}
        if result:  # 命中结果稳定可缓存；未下载的附件稍后可能出现，miss 不缓存。
            self._hardlink_cache[cache_key] = result
        return result

    def _month(self, row: dict) -> str:
        dirs = self._directories()
        for field in ("dir1", "dir2"):
            value = dirs.get(_int(row.get(field)), "")
            if re.fullmatch(r"20\d{2}-\d{2}", value):
                return value
        return ""

    def enrich(self, message: dict) -> dict:
        result = dict(message)
        kind = result.get("kind")
        md5 = str(result.get("md5", "") or "")
        if kind == "emoji" and result.get("remote_url"):
            return result
        dirs = self._directories()
        if kind in {"image", "emoji"} and _HEX32.fullmatch(md5):
            row = self._hardlink("image_hardlink_info_v4", f"md5='{md5.lower()}'")
            file_name = str(row.get("file_name", "") or "")
            result["media_path"] = self._validated(
                "msg", "attach", dirs.get(_int(row.get("dir1")), ""),
                dirs.get(_int(row.get("dir2")), ""), "Img", file_name)
        elif kind == "video" and _HEX32.fullmatch(md5):
            row = self._hardlink(
                "video_hardlink_info_v4", f"md5='{md5.lower()}' AND file_name LIKE '%.mp4'")
            month = self._month(row)
            file_name = str(row.get("file_name", "") or "")
            result["media_path"] = self._validated("msg", "video", month, file_name)
            thumb = self._hardlink(
                "video_hardlink_info_v4", f"md5='{md5.lower()}' AND file_name LIKE '%.jpg'")
            result["thumb_path"] = self._validated(
                "msg", "video", self._month(thumb), str(thumb.get("file_name", "") or ""))
        elif kind == "file":
            file_name = str(result.get("file_name", "") or "")
            if file_name and Path(file_name).name == file_name:
                row = self._hardlink(
                    "file_hardlink_info_v4", f"file_name='{self._quote(file_name)}'")
                indexed_name = str(row.get("file_name", "") or "")
                month = self._month(row)
                result["media_path"] = self._validated("msg", "file", month, indexed_name)
        return result


def message_signature(message: dict) -> tuple:
    """聊天实时刷新签名：媒体路径/卡片内容变化时也会触发重绘。"""
    return tuple(message.get(key) for key in (
        "raw_type", "kind", "text", "title", "url", "media_path", "thumb_path",
        "quote_sender", "quote_text", "quote_type",
        "sender_id", "sender_name", "is_self", "ts"))
