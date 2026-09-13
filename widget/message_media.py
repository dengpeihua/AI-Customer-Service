"""工作台共享的本地媒体与消息刷新辅助函数。"""
from __future__ import annotations

from pathlib import Path


_SAFE_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


def is_standard_image(path: str | Path) -> bool:
    candidate = Path(path)
    return candidate.suffix.lower() in _SAFE_IMAGE_EXTENSIONS and candidate.is_file()


def message_signature(message: dict) -> tuple:
    return tuple(message.get(key) for key in (
        "local_id", "raw_type", "kind", "text", "title", "description",
        "display_time", "url", "media_path", "thumb_path",
        "media_url", "sender_avatar", "quote_sender", "quote_text", "quote_type",
        "sender_id", "sender_name",
        "is_self", "ts",
    ))
