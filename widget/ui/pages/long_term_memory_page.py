"""Long-term customer profile view backed by consolidated Mem0 memories."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFrame, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from widget.ui.pages.operations_page import _AsyncPage


_TYPE_LABELS = {
    "profile": "画像", "fact": "事实", "preference": "偏好",
    "need": "需求", "commitment": "承诺", "note": "备注",
}
_SOURCE_LABELS = {"mem0": "Mem0 对话提取", "manual": "人工确认", "conversation": "对话"}


class LongTermMemoryPage(_AsyncPage):
    TYPES = ("fact", "preference", "need", "commitment", "note")

    def __init__(self, bridge, *, labels=None, channels=None, bridge_for=None):
        super().__init__()
        self.setObjectName("OpsPage")
        self.bridge = bridge
        self._labels = dict(labels or {})
        self._channels = list(channels or [])
        self._bridge_for = bridge_for
        self._contacts: list[dict] = []
        self._origin_bridge_key = ""
        self._profile_generation = 0
        self._requested_profile: tuple[str, str, str, int] | None = None
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        selector = QHBoxLayout()
        self._contacts_box = QComboBox()
        self._contacts_box.setMinimumWidth(300)
        self._contacts_box.currentIndexChanged.connect(self._contact_changed)
        self._channel = QComboBox()
        channel_rows = self._channels or [
            ("douyin#default", "抖音")
        ]
        for key, label in channel_rows:
            self._channel.addItem(label, key)
        self._channel.activated.connect(self._channel_activated)
        self._contact = QLineEdit()
        self._contact.setPlaceholderText("好友 ID")
        load = QPushButton("查看画像")
        load.setObjectName("Primary")
        load.clicked.connect(self._load_profile)
        refresh = QPushButton("刷新会话")
        refresh.setObjectName("Ghost")
        refresh.clicked.connect(self.refresh)
        selector.addWidget(self._contacts_box, 1)
        selector.addWidget(self._channel)
        selector.addWidget(self._contact, 1)
        selector.addWidget(load)
        selector.addWidget(refresh)
        root.addLayout(selector)

        profile = QFrame()
        profile.setObjectName("OpsEditor")
        profile_layout = QVBoxLayout(profile)
        self._profile_title = QLabel("尚未选择好友")
        self._profile_title.setObjectName("OpsCardValue")
        self._summary = QLabel("画像会在聊天经 Mem0 提取后出现")
        self._summary.setWordWrap(True)
        self._summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._tags = QLabel("标签：—")
        self._tags.setObjectName("OpsMuted")
        self._sync = QLabel("同步：未开始")
        self._sync.setObjectName("OpsStatus")
        profile_layout.addWidget(self._profile_title)
        profile_layout.addWidget(self._summary)
        profile_layout.addWidget(self._tags)
        profile_layout.addWidget(self._sync)
        root.addWidget(profile)

        self._table = QTableWidget(0, 6)
        self._table.setObjectName("OpsTable")
        self._table.setHorizontalHeaderLabels(["类型", "长期记忆", "来源", "重要度", "置顶", "更新时间"])
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._table.setWordWrap(True)
        root.addWidget(self._table, 1)

        manual = QFrame()
        manual.setObjectName("OpsEditor")
        manual_row = QHBoxLayout(manual)
        self._manual_type = QComboBox()
        for kind in self.TYPES:
            self._manual_type.addItem(_TYPE_LABELS[kind], kind)
        self._manual_content = QLineEdit()
        self._manual_content.setPlaceholderText("人工确认的长期记忆(会同步写入，参与真实召回)")
        self._importance = QDoubleSpinBox()
        self._importance.setRange(0, 1)
        self._importance.setSingleStep(0.1)
        self._importance.setValue(0.7)
        add = QPushButton("新增并同步到长期记忆")
        add.clicked.connect(self._create)
        manual_row.addWidget(self._manual_type)
        manual_row.addWidget(self._manual_content, 1)
        manual_row.addWidget(QLabel("重要度"))
        manual_row.addWidget(self._importance)
        manual_row.addWidget(add)
        root.addWidget(manual)

    def refresh(self, *_args) -> None:
        self._sync.setText("正在读取已形成画像的好友…")
        keys = [key for key, _label in self._channels] or [self._channel_key()]
        self._start(lambda: self._load_all_contacts(keys), self._apply_contacts,
                    lambda message: self._sync.setText(f"读取失败：{message}"))

    def _bridge_for_key(self, channel_key: str):
        return self._bridge_for(channel_key) if self._bridge_for is not None else self.bridge

    def _channel_key(self) -> str:
        return str(self._channel.currentData() or self._channel.currentText())

    def _channel_activated(self, _index: int) -> None:
        self._origin_bridge_key = self._channel_key()
        self._profile_generation += 1
        self._requested_profile = None

    def _load_all_contacts(self, keys: list[str]) -> list[dict]:
        rows: list[dict] = []
        seen_bridges: set[int] = set()
        for key in keys:
            bridge = self._bridge_for_key(key)
            if id(bridge) in seen_bridges:
                continue
            seen_bridges.add(id(bridge))
            rows.extend(dict(row, _bridge_key=key) for row in (bridge.list_memory_contacts() or []))
        return rows

    def _apply_contacts(self, value: object) -> None:
        self._contacts = list(value or [])
        self._contacts_box.clear()
        for row in self._contacts:
            channel = str(row.get("channel") or "")
            name = row.get("display_name") or row.get("contact_id")
            self._contacts_box.addItem(
                f"{name}  ·  {self._labels.get(channel, channel)}", row
            )
        if self._contacts:
            self._contact_changed(0)
            self._load_profile()
        else:
            self._sync.setText("还没有长期画像，请先在“记忆对话”中勾选聊天并提取")

    def _contact_changed(self, index: int) -> None:
        row = self._contacts_box.itemData(index)
        if not isinstance(row, dict):
            return
        channel = str(row.get("channel") or "douyin#default")
        index = self._channel.findData(channel)
        if index < 0:
            self._channel.addItem(self._labels.get(channel, channel), channel)
            index = self._channel.count() - 1
        self._channel.setCurrentIndex(index)
        self._origin_bridge_key = str(row.get("_bridge_key") or channel)
        self._contact.setText(str(row.get("contact_id") or ""))
        self._profile_generation += 1
        self._requested_profile = None

    def _load_profile(self) -> None:
        contact_id = self._contact.text().strip()
        if not contact_id:
            self._sync.setText("请先选择好友/群聊或输入会话 ID")
            return
        self._sync.setText("正在读取 Mem0 长期画像…")
        channel = self._channel_key()
        bridge_key = self._origin_bridge_key or channel
        bridge = self._bridge_for_key(bridge_key)
        self._profile_generation += 1
        token = (bridge_key, channel, contact_id, self._profile_generation)
        self._requested_profile = token
        started = self._start(
            lambda: bridge.get_long_term_profile(channel, contact_id),
            lambda value, request=token: self._profile_ready(request, value),
            lambda message, request=token: self._profile_failed(request, message),
        )
        if not started:
            self._sync.setText("上一项记忆任务尚未完成，请稍后再次查看画像")

    def _profile_ready(self, token: tuple[str, str, str, int], value: object) -> None:
        if token != self._requested_profile:
            return
        self._apply_profile(value)

    def _profile_failed(self, token: tuple[str, str, str, int], message: str) -> None:
        if token == self._requested_profile:
            self._sync.setText(f"画像读取失败：{message}")

    def _apply_profile(self, value: object) -> None:
        payload = dict(value or {})
        self._profile_title.setText(
            f"{payload.get('display_name') or payload.get('contact_id')} · 长期画像"
        )
        self._summary.setText(payload.get("summary") or "暂无稳定画像")
        tags = payload.get("tags") or []
        self._tags.setText("画像维度：" + (" · ".join(tags) if tags else "暂无"))
        sync = payload.get("sync") or {}
        self._sync.setText(
            f"同步状态：{sync.get('status', 'unknown')} · 已处理 {sync.get('messages_processed', 0)} 条聊天 · "
            f"最近同步 {sync.get('last_synced_at') or '—'}"
        )
        rows = list(payload.get("memories") or [])
        self._table.clearSpans()
        if not rows:
            processed = int(sync.get("messages_processed") or 0)
            if processed:
                explanation = (
                    "已处理的聊天尚未形成长期记忆。请回到“记忆对话”，重新勾选包含明确事实、"
                    "偏好、需求、计划或承诺的消息并提取。"
                )
                self._sync.setText(self._sync.text() + " · 未形成长期记忆，可重新提取")
            else:
                explanation = (
                    "尚无长期记忆。请先在“记忆对话”中勾选客户明确表达的信息并提取，"
                    "或在下方新增一条人工确认记忆。"
                )
            self._table.setRowCount(1)
            item = QTableWidgetItem(explanation)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(0, 0, item)
            self._table.setSpan(0, 0, 1, self._table.columnCount())
            self._table.resizeRowsToContents()
            return
        self._table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            values = [
                _TYPE_LABELS.get(str(row.get("memory_type")), row.get("memory_type")),
                row.get("content"), _SOURCE_LABELS.get(str(row.get("source")), row.get("source")),
                f"{float(row.get('importance') or 0):.1f}",
                "是" if row.get("is_pinned") else "否", row.get("updated_at") or "",
            ]
            for column, cell in enumerate(values):
                item = QTableWidgetItem(str(cell or ""))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(row_index, column, item)
        self._table.resizeRowsToContents()

    def _create(self) -> None:
        contact_id = self._contact.text().strip()
        content = self._manual_content.text().strip()
        if not contact_id or not content:
            self._sync.setText("好友 ID 和记忆内容不能为空")
            return
        values = {
            "channel": self._channel_key(), "contact_id": contact_id,
            "memory_type": self._manual_type.currentData(), "content": content,
            "importance": self._importance.value(),
        }
        self._sync.setText("正在写入 Mem0 并更新画像…")
        bridge = self._bridge_for_key(self._origin_bridge_key or str(values["channel"]))
        started = self._start(
            lambda: bridge.create_memory(values),
            lambda _value: self._created(),
            lambda message: self._sync.setText(f"新增失败：{message}"),
        )
        if not started:
            self._sync.setText("上一项记忆任务尚未完成，本次新增尚未提交")

    def _created(self) -> None:
        self._manual_content.clear()
        self._load_profile()
