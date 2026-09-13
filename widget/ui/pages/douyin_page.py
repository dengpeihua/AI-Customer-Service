from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget


class _Signals(QObject):
    completed = Signal(object)


class DouyinPage(QWidget):
    """Small operational view that never exposes browser cookies or profile contents."""

    def __init__(self, adapter) -> None:
        super().__init__()
        self._adapters = list(adapter) if isinstance(adapter, (list, tuple)) else [adapter]
        self._signals = _Signals(self)
        self._signals.completed.connect(self._scan_completed)

        layout = QVBoxLayout(self)
        title = QLabel("个人抖音私信")
        title.setObjectName("Title")
        layout.addWidget(title)
        warning = QLabel(
            "默认只扫描并生成草稿。人工手动回复需要该账号 send_enabled=true；"
            "AI 自动发送还必须同时开启全局 auto_send。只有页面出现本人消息后才记为成功。"
        )
        warning.setWordWrap(True)
        warning.setObjectName("Muted")
        layout.addWidget(warning)
        self._status = QLabel("正在等待浏览器状态…")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)
        buttons = QHBoxLayout()
        self._scan = QPushButton("立即扫描")
        self._scan.setObjectName("Primary")
        self._scan.clicked.connect(self.scan_now)
        refresh = QPushButton("刷新状态")
        refresh.clicked.connect(self.refresh)
        buttons.addWidget(self._scan)
        buttons.addWidget(refresh)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        layout.addStretch(1)
        self.refresh()

    def refresh(self) -> None:
        blocks = []
        for adapter in self._adapters:
            status = adapter.status_snapshot()
            login = "已登录" if status.get("credentials_valid") else "未登录"
            identity = "已绑定" if status.get("identity_verified") else "未绑定/不匹配"
            fingerprint = str(status.get("identity_fingerprint") or "—")
            browser = "已连接" if status.get("receive_connected") else "未连接"
            sending = "允许真实发送" if status.get("send_enabled") else "只读/草稿"
            error = str(status.get("last_error") or "无")
            blocks.append(
                f"账号：{status.get('display_name') or status.get('account_id')}\n"
                f"浏览器：{browser} · 登录：{login} · 身份：{identity} · 模式：{sending}\n"
                f"身份指纹：{fingerprint}\n"
                f"已发现会话：{status.get('sessions', 0)} · 最近状态：{error}"
            )
        self._status.setText("\n\n".join(blocks))

    def scan_now(self) -> None:
        self._scan.setEnabled(False)
        self._status.setText("正在扫描抖音私信页…")

        def worker() -> None:
            count = 0
            errors = []
            for adapter in self._adapters:
                try:
                    count += adapter.scan_now()
                except Exception as exc:  # noqa: BLE001 - one account must not hide the others
                    errors.append(f"{adapter.channel}: {exc}")
            if errors:
                self._signals.completed.emit({"error": "；".join(errors), "count": count})
            else:
                self._signals.completed.emit({"count": count})

        threading.Thread(target=worker, name="douyin-manual-scan", daemon=True).start()

    def _scan_completed(self, result: dict) -> None:
        self._scan.setEnabled(True)
        if result.get("error"):
            self._status.setText(f"扫描失败：{result['error']}")
            return
        self.refresh()
