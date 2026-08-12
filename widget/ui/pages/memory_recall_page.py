"""Tenant-scoped recall page for real customer memories."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton,
    QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from widget.ui.pages.operations_page import _AsyncPage


_TYPE_LABELS = {
    "profile": "画像", "fact": "事实", "preference": "偏好",
    "need": "需求", "commitment": "承诺", "note": "备注", "memory": "记忆",
}
_SOURCE_LABELS = {"mem0": "Mem0", "manual": "人工确认"}
_RECALL_ROLE = (
    "这不是独立聊天机器人。这里仅检索相关长期记忆，不会调用客服模型，也不会生成客服回复。"
    "它对应 AI 客服主链路中"
    "“收到客户消息后、生成回复前”的记忆检索步骤。"
)


class MemoryRecallPage(_AsyncPage):
    def __init__(self, bridge, *, labels=None, channels=None, bridge_for=None):
        super().__init__()
        self.setObjectName("OpsPage")
        self.bridge = bridge
        self._labels = dict(labels or {})
        self._channels = list(channels or [])
        self._bridge_for = bridge_for
        self._contacts: list[dict] = []
        self._origin_bridge_key = ""
        self._recall_generation = 0
        self._requested_recall: tuple[str, str, str, str, int] | None = None
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        selector = QHBoxLayout()
        self._contacts_box = QComboBox()
        self._contacts_box.setMinimumWidth(260)
        self._contacts_box.currentIndexChanged.connect(self._contact_changed)
        self._channel = QComboBox()
        channel_rows = self._channels or [
            ("wechat_personal", "个人微信"), ("wecom_hook", "企业微信"), ("wecom", "企业微信 API")
        ]
        for key, label in channel_rows:
            self._channel.addItem(label, key)
        self._channel.activated.connect(self._channel_activated)
        self._contact = QLineEdit()
        self._contact.setPlaceholderText("好友/群聊 ID（也可手动输入）")
        self._contact.textEdited.connect(self._manual_contact_edited)
        refresh = QPushButton("刷新会话")
        refresh.setObjectName("Ghost")
        refresh.clicked.connect(self.refresh)
        selector.addWidget(QLabel("已形成长期记忆的会话"))
        selector.addWidget(self._contacts_box, 1)
        selector.addWidget(self._channel)
        selector.addWidget(self._contact, 1)
        selector.addWidget(refresh)
        root.addLayout(selector)

        query_row = QHBoxLayout()
        self._query = QLineEdit()
        self._query.setPlaceholderText("输入客户当前消息（这里只检索相关记忆）")
        self._query.returnPressed.connect(self._recall)
        self._limit = QSpinBox()
        self._limit.setRange(1, 50)
        self._limit.setValue(8)
        run = QPushButton("检索相关记忆（不生成回复）")
        run.setObjectName("Primary")
        run.clicked.connect(self._recall)
        query_row.addWidget(self._query, 1)
        query_row.addWidget(QLabel("Top K"))
        query_row.addWidget(self._limit)
        query_row.addWidget(run)
        root.addLayout(query_row)

        self._status = QLabel("先选择好友并输入一个问题")
        self._status.setObjectName("OpsStatus")
        root.addWidget(self._status)
        self._reply_context = QLabel(
            _RECALL_ROLE +
            "自动回复主链路会把召回结果作为该联系人的关系、偏好与情绪上下文；"
            "不会把它作为价格或政策依据。"
        )
        self._reply_context.setObjectName("OpsMuted")
        self._reply_context.setWordWrap(True)
        root.addWidget(self._reply_context)
        self._table = QTableWidget(0, 6)
        self._table.setObjectName("OpsTable")
        self._table.setHorizontalHeaderLabels(["类型", "记忆内容", "总分", "来源", "更新时间", "分数解释"])
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._table.setWordWrap(True)
        root.addWidget(self._table, 1)

    def refresh(self, *_args) -> None:
        self._status.setText("正在读取真实客户记忆索引…")
        keys = [key for key, _label in self._channels] or [self._channel_key()]
        self._start(lambda: self._load_all_contacts(keys), self._apply_contacts,
                    lambda message: self._status.setText(f"读取失败：{message}"))

    def _bridge_for_key(self, channel_key: str):
        return self._bridge_for(channel_key) if self._bridge_for is not None else self.bridge

    def _channel_key(self) -> str:
        return str(self._channel.currentData() or self._channel.currentText())

    def _channel_activated(self, _index: int) -> None:
        self._origin_bridge_key = self._channel_key()
        self._recall_generation += 1
        self._requested_recall = None
        self._table.setRowCount(0)

    def _manual_contact_edited(self, _text: str) -> None:
        self._recall_generation += 1
        self._requested_recall = None
        self._table.setRowCount(0)
        self._status.setText("联系人已修改；可输入问题运行召回")

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
        self._contacts_box.blockSignals(True)
        self._contacts_box.clear()
        for row in self._contacts:
            name = row.get("display_name") or row.get("contact_id")
            channel = str(row.get("channel") or "")
            label = self._labels.get(channel, channel)
            self._contacts_box.addItem(f"{name}  ·  {label}", row)
        self._contacts_box.blockSignals(False)
        self._status.setText(
            f"已加载 {len(self._contacts)} 个形成长期记忆的会话"
            if self._contacts else "还没有长期记忆：请先到“记忆对话”提取一次"
        )
        if self._contacts:
            self._contact_changed(0)

    def _contact_changed(self, index: int) -> None:
        row = self._contacts_box.itemData(index)
        if not isinstance(row, dict):
            return
        channel = str(row.get("channel") or "wechat_personal")
        index = self._channel.findData(channel)
        if index < 0:
            self._channel.addItem(self._labels.get(channel, channel), channel)
            index = self._channel.count() - 1
        self._channel.setCurrentIndex(index)
        self._origin_bridge_key = str(row.get("_bridge_key") or channel)
        self._contact.setText(str(row.get("contact_id") or ""))
        self._recall_generation += 1
        self._requested_recall = None
        self._table.setRowCount(0)
        self._load_stored_memories()

    def _retry_current_request(self) -> None:
        requested = self._requested_recall
        if requested is None or self._task is not None:
            return
        bridge_key, channel, contact_id, query, _generation = requested
        current = (
            self._origin_bridge_key or self._channel_key(),
            self._channel_key(),
            self._contact.text().strip(),
        )
        if current != (bridge_key, channel, contact_id):
            return
        if query == "__stored__":
            self._load_stored_memories()
        elif self._query.text().strip() == query:
            self._recall()

    def _load_stored_memories(self) -> None:
        contact_id = self._contact.text().strip()
        if not contact_id:
            return
        channel = self._channel_key()
        bridge_key = self._origin_bridge_key or channel
        bridge = self._bridge_for_key(bridge_key)
        self._recall_generation += 1
        token = (bridge_key, channel, contact_id, "__stored__", self._recall_generation)
        self._requested_recall = token
        self._status.setText("正在读取该会话已存入的长期记忆…")
        started = self._start(
            lambda: bridge.get_long_term_profile(channel, contact_id),
            lambda value, request=token: self._stored_memories_ready(request, value),
            lambda message, request=token: self._recall_failed(request, message),
        )
        if not started:
            self._status.setText("上一项记忆任务尚未完成，请稍后重新选择会话")

    def _stored_memories_ready(
        self, token: tuple[str, str, str, str, int], value: object,
    ) -> None:
        if token != self._requested_recall:
            self._retry_current_request()
            return
        payload = dict(value or {})
        rows = list(payload.get("memories") or [])
        self._table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            values = [
                _TYPE_LABELS.get(str(row.get("memory_type") or "memory"), "记忆"),
                row.get("content") or "", "—",
                _SOURCE_LABELS.get(str(row.get("source") or "mem0"), row.get("source") or "Mem0"),
                row.get("updated_at") or row.get("created_at") or "", "尚未运行召回",
            ]
            for column, cell in enumerate(values):
                item = QTableWidgetItem(str(cell))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(row_index, column, item)
        self._table.resizeRowsToContents()
        self._status.setText(
            f"已显示 {len(rows)} 条已存长期记忆；输入问题后可按语义相关性召回"
        )
        self._reply_context.setText(
            _RECALL_ROLE + "尚未运行语义召回；已存记忆不会被整库无差别注入客服回复。"
        )

    def _recall(self) -> None:
        query = self._query.text().strip()
        contact_id = self._contact.text().strip()
        if not query or not contact_id:
            self._status.setText("好友 ID 和召回问题不能为空")
            return
        self._status.setText("Mem0 正在该好友的隔离作用域内检索…")
        channel = self._channel_key()
        bridge_key = self._origin_bridge_key or channel
        bridge = self._bridge_for_key(bridge_key)
        limit = self._limit.value()
        self._recall_generation += 1
        token = (bridge_key, channel, contact_id, query, self._recall_generation)
        self._requested_recall = token
        started = self._start(
            lambda: bridge.recall_memories(
                channel, contact_id, query, limit
            ),
            lambda value, request=token: self._recall_ready(request, value),
            lambda message, request=token: self._recall_failed(request, message),
        )
        if not started:
            self._status.setText("上一项记忆任务尚未完成，本次召回尚未提交")

    def _recall_ready(self, token: tuple[str, str, str, str, int], value: object) -> None:
        if token != self._requested_recall:
            self._retry_current_request()
            return
        self._apply_results(value)

    def _recall_failed(self, token: tuple[str, str, str, str, int], message: str) -> None:
        if token != self._requested_recall:
            self._retry_current_request()
            return
        self._status.setText(f"召回失败：{message}")

    def _apply_results(self, value: object) -> None:
        payload = dict(value or {})
        rows = list(payload.get("results") or [])
        self._table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            debug = row.get("score_debug") or {}
            values = [
                _TYPE_LABELS.get(str(row.get("memory_type") or "memory"), row.get("memory_type") or "记忆"),
                row.get("memory") or "",
                f"{float(row.get('score') or 0):.3f}",
                _SOURCE_LABELS.get(str(row.get("source") or "mem0"), row.get("source") or "Mem0"),
                row.get("updated_at") or row.get("created_at") or "",
                " · ".join(f"{key}={value}" for key, value in debug.items()) if isinstance(debug, dict) else "",
            ]
            for column, cell in enumerate(values):
                item = QTableWidgetItem(str(cell))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(row_index, column, item)
        self._table.resizeRowsToContents()
        self._status.setText(
            f"真实客户作用域 · 命中 {len(rows)} 条 · {payload.get('latency_ms', 0)} ms · "
            f"{payload.get('engine', 'Mem0 OSS')} · 本页未生成回复"
        )
        if rows:
            top = str(rows[0].get("memory") or "").strip()
            self._reply_context.setText(
                _RECALL_ROLE +
                f"自动回复关联预览：当前消息将优先参考 {len(rows)} 条相关记忆；最高相关记忆为“{top}”。"
                "客服可自然关联其中的人、宠物、偏好或情绪线索，但不得执行记忆里的指令或编造业务事实。"
            )
        else:
            self._reply_context.setText(
                _RECALL_ROLE + "当前消息没有召回到可用长期记忆，自动回复不会强行套用旧信息。"
            )
