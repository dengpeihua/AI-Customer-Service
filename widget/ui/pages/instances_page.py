from __future__ import annotations
from typing import Callable, Optional
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget,
                               QTableWidgetItem, QCheckBox, QPushButton, QHeaderView)

_PLATFORM_LABEL = {"wecom": "企微", "wechat": "微信"}

class InstancesConsolePage(QWidget):
    def __init__(self, supervisor, *, on_open_chat: Optional[Callable[[str], None]] = None,
                 on_new_instance: Optional[Callable[[str], None]] = None,
                 mem_warn_total_mb: int = 16384):
        super().__init__()
        self._sup = supervisor
        self._on_open_chat = on_open_chat or (lambda ck: None)
        self._on_new = on_new_instance or (lambda platform: None)
        self._warn_mb = mem_warn_total_mb
        self._rows: list = []                            # 与表行同序的 InstanceState
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        t = QLabel("实例"); t.setObjectName("Title"); top.addWidget(t)
        top.addStretch(1)
        b = QPushButton("+ 新建企微"); b.setObjectName("Primary")
        b.clicked.connect(lambda: self._on_new("wecom")); top.addWidget(b)
        bw = QPushButton("+ 新建微信"); bw.setObjectName("Ghost")
        bw.clicked.connect(lambda: self._on_new("wechat")); top.addWidget(bw)
        lay.addLayout(top)
        self._banner = QLabel(""); self._banner.setObjectName("Muted"); lay.addWidget(self._banner)
        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(
            ["账号", "平台", "状态", "进程内存MB（只读）", "AI接管", "操作"]
        )
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        lay.addWidget(self._table)
        self.refresh()

    def refresh(self) -> None:
        self._rows = self._sup.view()
        self._table.setRowCount(len(self._rows))
        for i, s in enumerate(self._rows):
            self._table.setItem(i, 0, QTableWidgetItem(s.display_name or s.account_id))
            self._table.setItem(i, 1, QTableWidgetItem(_PLATFORM_LABEL.get(s.platform, s.platform)))
            self._table.setItem(i, 2, QTableWidgetItem(s.status))
            mem_item = QTableWidgetItem(str(s.mem_mb))
            mem_item.setToolTip("该实例进程当前占用的运行内存，只能查看，不能在这里删除内容")
            self._table.setItem(i, 3, mem_item)
            cb = QCheckBox(); cb.setChecked(s.ai_enabled)
            cb.clicked.connect(lambda checked, ck=s.channel_key: self._sup.set_ai_enabled(ck, checked))
            self._table.setCellWidget(i, 4, cb)
            op = QWidget(); ol = QHBoxLayout(op); ol.setContentsMargins(0, 0, 0, 0)
            chat = QPushButton("看会话"); chat.setObjectName("Ghost")
            chat.clicked.connect(lambda _=False, ck=s.channel_key: self._on_open_chat(ck))
            ol.addWidget(chat)
            self._table.setCellWidget(i, 5, op)
        total = self._sup.total_mem_mb()
        if total > self._warn_mb:
            self._banner.setText(f"⚠️ 总内存占用 {total}MB 超过阈值 {self._warn_mb}MB，注意关掉不用的号")
            self._banner.setStyleSheet("color:#c0392b;")
        else:
            self._banner.setText(f"实例进程总内存 {total}MB（只读）"); self._banner.setStyleSheet("")

    # —— 测试可调的薄接口（不依赖真点击）——
    def row_count(self) -> int:
        return self._table.rowCount()
    def set_ai_for_row(self, row: int, enabled: bool) -> None:
        self._sup.set_ai_enabled(self._rows[row].channel_key, enabled)
    def open_chat_for_row(self, row: int) -> None:
        self._on_open_chat(self._rows[row].channel_key)
    def is_mem_warning(self) -> bool:
        return self._sup.total_mem_mb() > self._warn_mb
