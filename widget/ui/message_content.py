"""聊天气泡中的结构化消息渲染器。

所有来自抖音私信的文字都按纯文本显示；链接只允许 http/https，本地附件只在用户点击后打开。
"""
from __future__ import annotations

import threading
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

from widget.message_media import is_standard_image


_CARD_LABELS = {
    "image": "图片", "emoji": "表情包", "file": "文件", "video": "视频",
    "voice": "语音", "link": "链接", "forward": "转发的聊天记录",
    "miniapp": "小程序", "app_post": "应用帖子", "app": "应用消息",
    "quote": "引用消息", "system": "系统消息", "unknown": "其他消息",
}


def _safe_http_url(value: str) -> str:
    value = str(value or "").strip()
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    return value if parsed.scheme.lower() in {"http", "https"} and parsed.netloc else ""


def _plain_label(text: str, *, muted: bool = False) -> QLabel:
    label = QLabel(str(text or ""))
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setMaximumWidth(360)
    if muted:
        label.setStyleSheet("color: #68757D; font-size: 12px;")
    return label


def _emit_preview_safely(source, data, error: str) -> None:
    """后台结果晚于气泡销毁时直接丢弃，避免工作线程产生未捕获 Qt RuntimeError。"""
    try:
        source._preview_ready.emit(data, error)
    except RuntimeError:
        pass


def _emit_remote_image_safely(source, data, error: str) -> None:
    """Ignore a late image download after its target widget has been destroyed."""
    try:
        source._loaded.emit(data, error)
    except RuntimeError:
        pass


class RemoteImageLabel(QLabel):
    """Bounded asynchronous HTTP image preview used by message media and avatars."""

    _loaded = Signal(object, str)

    def __init__(self, url: str, *, width: int, height: int, parent=None):
        super().__init__(parent)
        self._width = width
        self._height = height
        self.setFixedSize(width, height)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setText("…")
        self._loaded.connect(self._apply)
        if is_standard_image(url):
            try:
                self._apply(Path(url).read_bytes(), "")
            except OSError:
                self.setText("图片暂不可用")
            return
        safe_url = _safe_http_url(url)
        if safe_url:
            threading.Thread(
                target=self._download, args=(safe_url,), daemon=True,
                name="douyin-image-preview",
            ).start()

    def _download(self, url: str) -> None:
        try:
            with httpx.Client(timeout=8.0, follow_redirects=True, trust_env=False) as client:
                response = client.get(url)
                response.raise_for_status()
                data = response.content
            if len(data) > 8 * 1024 ** 2:
                raise ValueError("image too large")
            _emit_remote_image_safely(self, data, "")
        except Exception:
            _emit_remote_image_safely(self, None, "图片暂不可用")

    def _apply(self, data, error: str) -> None:
        if not data:
            self.setText(error)
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            self.setText("图片暂不可用")
            return
        self.setText("")
        self.setPixmap(pixmap.scaled(
            self._width, self._height, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))


class MessageContent(QWidget):
    """一条消息的内容区；耗时图片解密/下载在后台执行。"""

    _preview_ready = Signal(object, str)

    def __init__(self, message: dict, parent=None):
        super().__init__(parent)
        self.message = dict(message or {})
        self.setMaximumWidth(380)
        self.setObjectName("BubbleOut" if self.message.get("is_self") else "BubbleIn")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(12, 8, 12, 8)
        self._layout.setSpacing(5)
        self._preview = None
        self._retry = None
        self._preview_ready.connect(self._apply_preview)
        self._build()

    def _build(self) -> None:
        kind = str(self.message.get("kind", "text") or "text")
        if kind == "text":
            self._layout.addWidget(_plain_label(self.message.get("text", "")))
            return
        if kind in {"image", "emoji"}:
            self._build_image(kind)
            return
        if kind == "video":
            self._layout.addWidget(_plain_label("视频消息"))
            title = str(self.message.get("title") or self.message.get("text") or "").strip()
            if title and title != "[视频]":
                self._layout.addWidget(_plain_label(title, muted=True))
            self._add_image_path(str(self.message.get("thumb_path", "") or ""))
            remote_preview = _safe_http_url(self.message.get("media_url", ""))
            if remote_preview:
                self._layout.addWidget(RemoteImageLabel(remote_preview, width=320, height=220))
            self._add_duration()
            if self.message.get("media_path"):
                self._add_local_open("播放视频")
            else:
                self._add_url_open("播放视频", value=self.message.get("video_url", ""))
            return
        if kind == "voice":
            self._layout.addWidget(_plain_label("▶ 语音消息"))
            self._add_duration()
            self._layout.addWidget(_plain_label("已捕获消息类型；当前抖音私信本地语音编码暂不支持直接播放。", muted=True))
            return
        if kind == "file":
            self._layout.addWidget(_plain_label(self.message.get("title") or self.message.get("text")))
            size = int(self.message.get("file_size") or 0)
            if size:
                self._layout.addWidget(_plain_label(self._format_size(size), muted=True))
            self._add_local_open("打开文件")
            return
        if kind == "quote":
            self._layout.addWidget(_plain_label(self.message.get("text", "")))
            sender = str(self.message.get("quote_sender", "") or "").strip()
            self._layout.addWidget(_plain_label(f"引用 {sender}" if sender else "引用消息", muted=True))
            quote = _plain_label(self.message.get("quote_text", ""), muted=True)
            quote.setObjectName("QuoteBlock")
            quote.setStyleSheet(
                "QLabel#QuoteBlock { color: #68757D; font-size: 12px; "
                "background: #F1F4F5; border-left: 3px solid #B9C3C8; "
                "padding: 6px; }"
            )
            self._layout.addWidget(quote)
            remote = _safe_http_url(self.message.get("media_url", ""))
            if remote:
                self._layout.addWidget(RemoteImageLabel(remote, width=320, height=220))
            else:
                self._add_image_path(str(self.message.get("media_path", "") or ""))
            return

        self._layout.addWidget(_plain_label(_CARD_LABELS.get(kind, "应用消息"), muted=True))
        self._layout.addWidget(_plain_label(self.message.get("title") or self.message.get("text")))
        description = str(self.message.get("description", "") or "").strip()
        if description:
            self._layout.addWidget(_plain_label(description, muted=True))
        remote_preview = _safe_http_url(self.message.get("media_url", ""))
        if remote_preview:
            self._layout.addWidget(RemoteImageLabel(remote_preview, width=320, height=220))
        self._add_url_open("打开内容")

    def _build_image(self, kind: str) -> None:
        self._layout.addWidget(_plain_label(_CARD_LABELS[kind], muted=True))
        self._preview = QLabel()
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setMinimumSize(180, 90)
        self._preview.setMaximumWidth(340)
        self._preview.setText("正在读取预览…")
        self._layout.addWidget(self._preview)
        path = str(self.message.get("media_path", "") or "")
        if path:
            try:
                if is_standard_image(path):
                    self._apply_preview(Path(path).read_bytes(), "")
                else:
                    self._preview.setText("该媒体格式暂不支持直接预览。")
            except Exception as exc:
                self._preview.setText("图片已捕获，但当前格式暂不可预览。")
                self._retry = QPushButton("重新读取预览")
                self._retry.setToolTip(str(exc))
                self._retry.clicked.connect(self._retry_preview)
                self._layout.addWidget(self._retry)
            return
        remote = _safe_http_url(
            self.message.get("media_url") or self.message.get("remote_url", "")
        )
        if remote:
            preview = RemoteImageLabel(remote, width=320, height=220)
            self._layout.replaceWidget(self._preview, preview)
            self._preview.deleteLater()
            self._preview = preview
        else:
            self._preview.setText("已识别这条图片消息，但尚未找到可用的本地预览。")

    def _add_image_path(self, path: str) -> None:
        if not path or not Path(path).is_file():
            return
        label = QLabel()
        pixmap = QPixmap(path)
        if not pixmap.isNull():
            label.setPixmap(pixmap.scaled(
                320, 190, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
            self._layout.addWidget(label)

    def _retry_preview(self) -> None:
        path = str(self.message.get("media_path", "") or "")
        if not path or self._retry is None:
            return
        self._retry.setEnabled(False)
        self._retry.setText("正在重新读取预览…")

        def worker() -> None:
            try:
                if not is_standard_image(path):
                    raise ValueError("unsupported enterprise media format")
                data = Path(path).read_bytes()
                error = ""
            except Exception:
                data = None
                error = "当前媒体格式暂不可预览。"
            _emit_preview_safely(self, data, error)

        threading.Thread(target=worker, daemon=True, name="channel-image-preview").start()

    def _download_emoji(self, url: str) -> None:
        def worker() -> None:
            try:
                with httpx.Client(timeout=8.0, follow_redirects=True, trust_env=False) as client:
                    response = client.get(url)
                    response.raise_for_status()
                    data = response.content
                if len(data) > 5 * 1024 ** 2:
                    raise ValueError("表情预览超过 5MB")
                error = ""
            except Exception:
                data = None
                error = "表情消息已捕获，但在线预览暂时不可用。"
            _emit_preview_safely(self, data, error)

        threading.Thread(target=worker, daemon=True, name="channel-emoji-preview").start()

    def _apply_preview(self, data, error: str) -> None:
        if self._preview is None:
            return
        if not data:
            self._preview.setText(error or "预览不可用")
            if self._retry is not None:
                self._retry.setEnabled(True)
                self._retry.setText("重新读取预览")
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(bytes(data)):
            self._preview.setText("预览数据不是受支持的图片格式。")
            return
        self._preview.setText("")
        self._preview.setPixmap(pixmap.scaled(
            340, 260, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))
        if self._retry is not None:
            self._retry.hide()

    def _add_duration(self) -> None:
        duration_ms = int(self.message.get("duration_ms") or 0)
        if duration_ms:
            self._layout.addWidget(_plain_label(f"{duration_ms / 1000:.1f} 秒", muted=True))

    def _add_local_open(self, label: str) -> None:
        path = str(self.message.get("media_path", "") or "")
        if not path or not Path(path).is_file():
            self._layout.addWidget(_plain_label("本地附件尚未下载完成或已被抖音私信清理。", muted=True))
            return
        button = QPushButton(label)
        button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(path)))
        self._layout.addWidget(button)

    def _add_url_open(self, label: str, *, value: object = "") -> None:
        url = _safe_http_url(value or self.message.get("url", ""))
        if not url:
            return
        button = QPushButton(label)
        button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(url)))
        self._layout.addWidget(button)

    @staticmethod
    def _format_size(size: int) -> str:
        if size < 1024:
            return f"{size} B"
        if size < 1024 ** 2:
            return f"{size / 1024:.1f} KB"
        if size < 1024 ** 3:
            return f"{size / 1024 ** 2:.1f} MB"
        return f"{size / 1024 ** 3:.1f} GB"


def message_content_widget(message: dict, parent=None) -> QWidget:
    return MessageContent(message, parent)
