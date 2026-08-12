"""Memory records and tenant-scoped operations pages for the desktop workbench."""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMessageBox, QPushButton, QSpinBox, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)


_MEMORY_TYPE_LABELS = {
    "profile": "画像", "fact": "事实", "preference": "偏好",
    "need": "需求", "commitment": "承诺", "note": "备注",
}
_MEMORY_SOURCE_LABELS = {"mem0": "Mem0 对话提取", "manual": "人工确认", "conversation": "对话"}


class _Signals(QObject):
    completed = Signal(object)
    failed = Signal(str)


class _Task(QRunnable):
    def __init__(self, callback: Callable[[], object]):
        super().__init__()
        self.callback = callback
        self.signals = _Signals()

    @Slot()
    def run(self) -> None:
        try:
            result = self.callback()
        except Exception as exc:  # noqa: BLE001 - UI reports companion failures inline
            self.signals.failed.emit(str(exc))
            return
        self.signals.completed.emit(result)


def _item(value: object) -> QTableWidgetItem:
    item = QTableWidgetItem("" if value is None else str(value))
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    return item


def _table(headers: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setObjectName("OpsTable")
    table.setHorizontalHeaderLabels(headers)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    table.setAlternatingRowColors(True)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setStretchLastSection(True)
    return table


class _AsyncPage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._task: _Task | None = None

    def _start(self, callback: Callable[[], object], completed: Callable[[object], None],
               failed: Callable[[str], None]) -> bool:
        if self._task is not None:
            return False
        task = _Task(callback)
        task.signals.completed.connect(lambda value: self._finish(task, completed, value))
        task.signals.failed.connect(lambda message: self._fail(task, failed, message))
        self._task = task
        QThreadPool.globalInstance().start(task)
        return True

    def _finish(self, task: _Task, callback: Callable[[object], None], value: object) -> None:
        if self._task is task:
            self._task = None
        callback(value)

    def _fail(self, task: _Task, callback: Callable[[str], None], message: str) -> None:
        if self._task is task:
            self._task = None
        callback(message)


class MemoryRecordsPage(_AsyncPage):
    TYPES = ("", "profile", "fact", "preference", "need", "commitment", "note")

    def __init__(self, bridge, *, governance: bool = False, channels=None,
                 bridge_for=None, labels=None):
        super().__init__()
        self.setObjectName("OpsPage")
        self.bridge = bridge
        self.governance = governance
        self._channels = list(channels or [])
        self._bridge_for = bridge_for
        self._labels = dict(labels or {})
        self._rows: list[dict] = []
        self._history_generation = 0
        self._history_request: tuple[str, int, int] | None = None
        # Per-memory Mem0 history is advisory/read-only. It must never occupy the
        # main mutation slot, otherwise selecting a row and immediately pressing
        # Delete silently rejects the memory deletion while history is loading.
        self._history_task: _Task | None = None
        self._pending_history_row: dict | None = None
        self._memory_select_all = None
        self._memory_clear_all = None
        self._memory_delete_selected = None
        self._memory_selection_status = None
        self._decision_rows: list[dict] = []
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        filters = QHBoxLayout()
        self._search = QLineEdit()
        self._search.setPlaceholderText("搜索客户 ID 或记忆内容")
        self._type = QComboBox()
        for value in self.TYPES:
            self._type.addItem("全部类型" if not value else _MEMORY_TYPE_LABELS[value], value)
        refresh = QPushButton("刷新")
        refresh.setObjectName("Ghost")
        refresh.clicked.connect(self.refresh)
        filters.addWidget(self._search, 1)
        filters.addWidget(self._type)
        filters.addWidget(refresh)
        root.addLayout(filters)

        self._stats = QLabel("等待加载")
        self._stats.setObjectName("OpsStatus")
        root.addWidget(self._stats)
        headers = ["选择", "ID", "渠道", "客户", "类型", "内容", "来源", "重要度", "置顶", "治理信号", "更新时间"]
        if not governance:
            headers = ["ID", "渠道", "客户", "类型", "内容", "重要度", "置顶", "更新时间"]
        self._table = _table(headers)
        self._table.itemSelectionChanged.connect(self._selection_changed)
        if governance:
            self._table.itemChanged.connect(self._memory_item_changed)

            memory_actions = QHBoxLayout()
            memory_title = QLabel("记忆内容")
            memory_title.setObjectName("OpsCardValue")
            self._memory_selection_status = QLabel("已选 0 条")
            self._memory_selection_status.setObjectName("OpsMuted")
            self._memory_select_all = QPushButton("全选")
            self._memory_select_all.setObjectName("Ghost")
            self._memory_select_all.clicked.connect(self._select_all_memories)
            self._memory_clear_all = QPushButton("取消全选")
            self._memory_clear_all.setObjectName("Ghost")
            self._memory_clear_all.clicked.connect(self._clear_all_memories)
            self._memory_delete_selected = QPushButton("删除")
            self._memory_delete_selected.setObjectName("DangerButton")
            self._memory_delete_selected.setEnabled(False)
            self._memory_delete_selected.clicked.connect(self._delete_selected_memories)
            memory_actions.addWidget(memory_title)
            memory_actions.addWidget(self._memory_selection_status)
            memory_actions.addStretch(1)
            memory_actions.addWidget(self._memory_select_all)
            memory_actions.addWidget(self._memory_clear_all)
            memory_actions.addWidget(self._memory_delete_selected)
            root.addLayout(memory_actions)
        root.addWidget(self._table, 1)

        editor = QFrame()
        editor.setObjectName("OpsEditor")
        row = QHBoxLayout(editor)
        self._channel = QComboBox()
        channel_rows = self._channels or [
            ("wechat_personal", "个人微信"), ("wecom_hook", "企业微信")
        ]
        for key, label in channel_rows:
            self._channel.addItem(label, key)
        self._contact = QLineEdit(); self._contact.setPlaceholderText("客户 ID")
        if governance:
            self._channel.setEnabled(False)
            self._contact.setReadOnly(True)
        self._edit_type = QComboBox()
        for value in self.TYPES[1:]: self._edit_type.addItem(_MEMORY_TYPE_LABELS[value], value)
        self._content = QLineEdit(); self._content.setPlaceholderText("长期记忆内容")
        self._importance = QDoubleSpinBox(); self._importance.setRange(0, 1); self._importance.setSingleStep(0.1); self._importance.setValue(0.5)
        row.addWidget(self._channel)
        row.addWidget(self._contact)
        row.addWidget(self._edit_type)
        row.addWidget(self._content, 1)
        row.addWidget(QLabel("重要度"))
        row.addWidget(self._importance)
        if governance:
            save = QPushButton("保存修改"); save.setObjectName("Primary"); save.clicked.connect(self._save)
            pin = QPushButton("切换置顶"); pin.clicked.connect(self._toggle_pin)
            row.addWidget(save); row.addWidget(pin)
        else:
            add = QPushButton("新增记忆"); add.setObjectName("Primary"); add.clicked.connect(self._create)
            row.addWidget(add)
        root.addWidget(editor)
        self._history_status = None
        self._history_table = None
        self._decision_select_all = None
        self._decision_clear_all = None
        self._decision_delete = None
        self._decision_selection_status = None
        if governance:
            decision_actions = QHBoxLayout()
            decision_title = QLabel("语义治理决策")
            decision_title.setObjectName("OpsCardValue")
            self._decision_selection_status = QLabel("已选 0 条")
            self._decision_selection_status.setObjectName("OpsMuted")
            self._decision_select_all = QPushButton("全选")
            self._decision_select_all.setObjectName("Ghost")
            self._decision_select_all.clicked.connect(self._select_all_decisions)
            self._decision_clear_all = QPushButton("取消全选")
            self._decision_clear_all.setObjectName("Ghost")
            self._decision_clear_all.clicked.connect(self._clear_all_decisions)
            self._decision_delete = QPushButton("删除所选")
            self._decision_delete.setObjectName("DangerButton")
            self._decision_delete.setEnabled(False)
            self._decision_delete.clicked.connect(self._delete_selected_decisions)
            decision_actions.addWidget(decision_title)
            decision_actions.addWidget(self._decision_selection_status)
            decision_actions.addStretch(1)
            decision_actions.addWidget(self._decision_select_all)
            decision_actions.addWidget(self._decision_clear_all)
            decision_actions.addWidget(self._decision_delete)
            root.addLayout(decision_actions)
            self._history_status = QLabel("选择治理决策后可批量删除；选择上方记忆可查看对应的 Mem0 治理轨迹")
            self._history_status.setObjectName("OpsStatus")
            root.addWidget(self._history_status)
            self._history_table = _table(["选择", "渠道", "客户", "事件", "变更前", "变更后", "决策原因", "时间"])
            self._history_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
            self._history_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
            self._history_table.itemChanged.connect(self._decision_item_changed)
            root.addWidget(self._history_table)

    def refresh(self, *_args) -> None:
        search, memory_type = self._search.text().strip(), str(self._type.currentData() or "")
        keys = [key for key, _label in self._channels] or [
            str(self._channel.currentData() or self._channel.currentText())
        ]
        self._stats.setText("正在加载长期记忆…")
        self._start(
            lambda: self._load_records(keys, search, memory_type),
            self._apply, lambda message: self._stats.setText(f"加载失败：{message}"),
        )

    def _bridge_for_key(self, channel_key: str):
        return self._bridge_for(channel_key) if self._bridge_for is not None else self.bridge

    def _load_records(self, keys: list[str], search: str, memory_type: str) -> dict:
        rows: list[dict] = []
        decisions: list[dict] = []
        total = pinned = 0
        by_type: dict[str, int] = {}
        seen_bridges: set[int] = set()
        for key in keys:
            bridge = self._bridge_for_key(key)
            if id(bridge) in seen_bridges:
                continue
            seen_bridges.add(id(bridge))
            rows.extend(
                dict(item, _bridge_key=key)
                for item in bridge.list_memories(
                    search=search, memory_type=memory_type, limit=200
                )
            )
            stats = bridge.memory_stats() or {}
            if self.governance and hasattr(bridge, "list_memory_decisions"):
                decision_payload = bridge.list_memory_decisions(limit=100) or {}
                decisions.extend(
                    dict(event, _bridge_key=key)
                    for event in (decision_payload.get("events") or [])
                )
            total += int(stats.get("total") or 0)
            pinned += int(stats.get("pinned") or 0)
            for kind, count in (stats.get("by_type") or {}).items():
                by_type[str(kind)] = by_type.get(str(kind), 0) + int(count)
        return {
            "rows": rows,
            "decisions": decisions,
            "stats": {"total": total, "pinned": pinned, "by_type": by_type},
        }

    def _apply(self, value: object) -> None:
        payload = dict(value or {})
        self._rows = list(payload.get("rows") or [])
        stats = payload.get("stats") or {}
        by_type = stats.get("by_type") or {}
        types = " · ".join(f"{key} {count}" for key, count in sorted(by_type.items()))
        duplicate_keys: dict[tuple[str, str, str, str], int] = {}
        for row in self._rows:
            key = (
                str(row.get("channel") or ""), str(row.get("contact_id") or ""),
                str(row.get("memory_type") or ""), " ".join(str(row.get("content") or "").lower().split()),
            )
            duplicate_keys[key] = duplicate_keys.get(key, 0) + 1
        review_count = 0
        for row in self._rows:
            key = (
                str(row.get("channel") or ""), str(row.get("contact_id") or ""),
                str(row.get("memory_type") or ""), " ".join(str(row.get("content") or "").lower().split()),
            )
            signals = []
            if duplicate_keys.get(key, 0) > 1: signals.append("疑似重复")
            if float(row.get("importance") or 0) < 0.4: signals.append("低重要度")
            if row.get("memory_type") == "profile": signals.append("旧画像兼容项")
            row["_governance"] = " · ".join(signals) if signals else "健康"
            if signals: review_count += 1
        prefix = f"待审阅 {review_count} 条 · " if self.governance else ""
        self._stats.setText(prefix + f"共 {stats.get('total', len(self._rows))} 条 · 置顶 {stats.get('pinned', 0)} 条" + (f" · {types}" if types else ""))
        self._table.blockSignals(True)
        self._table.setRowCount(len(self._rows))
        for i, row in enumerate(self._rows):
            values = [row.get("id"), row.get("channel"), row.get("contact_id"), _MEMORY_TYPE_LABELS.get(str(row.get("memory_type")), row.get("memory_type")),
                      row.get("content"), f"{float(row.get('importance') or 0):.1f}",
                      "是" if row.get("is_pinned") else "否", row.get("updated_at") or ""]
            if self.governance:
                values = [row.get("id"), row.get("channel"), row.get("contact_id"), _MEMORY_TYPE_LABELS.get(str(row.get("memory_type")), row.get("memory_type")),
                          row.get("content"), _MEMORY_SOURCE_LABELS.get(str(row.get("source")), row.get("source")), f"{float(row.get('importance') or 0):.1f}",
                          "是" if row.get("is_pinned") else "否", row.get("_governance"), row.get("updated_at") or ""]
                check = _item("")
                check.setFlags(check.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                check.setCheckState(Qt.CheckState.Unchecked)
                check.setData(Qt.ItemDataRole.UserRole, int(row.get("id") or 0))
                check.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._table.setItem(i, 0, check)
                for j, value in enumerate(values, start=1):
                    self._table.setItem(i, j, _item(value))
            else:
                for j, value in enumerate(values):
                    self._table.setItem(i, j, _item(value))
        self._table.blockSignals(False)
        self._table.resizeRowsToContents()
        if self.governance:
            self._update_memory_selection()
            self._apply_decisions(list(payload.get("decisions") or []))

    def _selected(self) -> dict | None:
        index = self._table.currentRow()
        return self._rows[index] if 0 <= index < len(self._rows) else None

    def _memory_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == 0:
            self._update_memory_selection()

    def _checked_memories(self) -> list[dict]:
        if not self.governance or len(self._rows) != self._table.rowCount():
            return []
        return [
            row for index, row in enumerate(self._rows)
            if self._table.item(index, 0) is not None
            and self._table.item(index, 0).checkState() == Qt.CheckState.Checked
        ]

    def _update_memory_selection(self) -> None:
        selected = len(self._checked_memories())
        if self._memory_selection_status is not None:
            self._memory_selection_status.setText(f"已选 {selected} 条")
        if self._memory_delete_selected is not None:
            self._memory_delete_selected.setEnabled(selected > 0)

    def _set_all_memories_checked(self, state: Qt.CheckState) -> None:
        if not self.governance or len(self._rows) != self._table.rowCount():
            return
        self._table.blockSignals(True)
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            if item is not None:
                item.setCheckState(state)
        self._table.blockSignals(False)
        self._update_memory_selection()

    def _select_all_memories(self) -> None:
        self._set_all_memories_checked(Qt.CheckState.Checked)

    def _clear_all_memories(self) -> None:
        self._set_all_memories_checked(Qt.CheckState.Unchecked)

    def _delete_selected_memories(self) -> None:
        selected = self._checked_memories()
        if not selected:
            self._stats.setText("请先勾选要删除的记忆内容")
            return
        if QMessageBox.question(
            self,
            "批量删除长期记忆",
            f"确定删除所选 {len(selected)} 条长期记忆？Mem0 中的对应记忆也会同步删除；"
            "由聊天提取的来源消息将允许重新勾选提取。此操作不可撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return

        self._pending_history_row = None
        self._history_request = None

        def remove_selected() -> dict:
            deleted = 0
            failures: list[str] = []
            for row in selected:
                bridge_key = str(row.get("_bridge_key") or row.get("channel") or "")
                memory_id = int(row["id"])
                try:
                    self._bridge_for_key(bridge_key).delete_memory(memory_id)
                except Exception as exc:  # noqa: BLE001 - report partial batch failures in the UI
                    failures.append(f"ID {memory_id}: {exc}")
                else:
                    deleted += 1
            return {"deleted": deleted, "failures": failures, "requested": len(selected)}

        if self._start(
            remove_selected,
            self._memory_delete_finished,
            lambda message: self._stats.setText(f"批量删除失败：{message}"),
        ):
            self._stats.setText(f"正在同步删除所选 {len(selected)} 条本地记录和 Mem0 记忆…")
        else:
            self._stats.setText("上一项记忆任务尚未完成，本次批量删除尚未提交")

    def _memory_delete_finished(self, value: object) -> None:
        result = dict(value or {})
        deleted = int(result.get("deleted") or 0)
        requested = int(result.get("requested") or 0)
        failures = list(result.get("failures") or [])
        if failures:
            self._stats.setText(
                f"已删除 {deleted}/{requested} 条，{len(failures)} 条失败：{failures[0]}；正在刷新…"
            )
        else:
            self._stats.setText(f"已删除 {deleted} 条长期记忆，正在刷新…")
        self.refresh()

    def _selection_changed(self) -> None:
        row = self._selected()
        if not row: return
        channel = str(row.get("channel") or "wechat_personal")
        index = self._channel.findData(channel)
        if index < 0:
            self._channel.addItem(self._labels.get(channel, channel), channel)
            index = self._channel.count() - 1
        self._channel.setCurrentIndex(index)
        self._contact.setText(str(row.get("contact_id") or ""))
        type_index = self._edit_type.findData(str(row.get("memory_type") or "note"))
        if type_index >= 0:
            self._edit_type.setCurrentIndex(type_index)
        self._content.setText(str(row.get("content") or ""))
        self._importance.setValue(float(row.get("importance") or 0.5))
        if self.governance:
            self._load_selected_history(row)

    def _load_selected_history(self, row: dict) -> None:
        if self._history_status is None:
            return
        if self._history_task is not None:
            self._pending_history_row = dict(row)
            self._history_status.setText("正在切换到所选记忆的治理轨迹…")
            return
        memory_id = int(row["id"])
        bridge_key = str(row.get("_bridge_key") or row.get("channel") or "")
        bridge = self._bridge_for_key(bridge_key)
        self._history_generation += 1
        request = (bridge_key, memory_id, self._history_generation)
        self._history_request = request
        self._history_status.setText("正在读取 Mem0 语义治理轨迹…")
        task = _Task(lambda: bridge.memory_history(memory_id))
        task.signals.completed.connect(
            lambda value: self._finish_history(task, request, value)
        )
        task.signals.failed.connect(
            lambda message: self._fail_history(task, request, message)
        )
        self._history_task = task
        QThreadPool.globalInstance().start(task)

    def _finish_history(
        self, task: _Task, request: tuple[str, int, int], value: object
    ) -> None:
        if self._history_task is task:
            self._history_task = None
        self._apply_history(request, value)
        self._start_pending_history()

    def _fail_history(
        self, task: _Task, request: tuple[str, int, int], message: str
    ) -> None:
        if self._history_task is task:
            self._history_task = None
        self._history_failed(request, message)
        self._start_pending_history()

    def _start_pending_history(self) -> None:
        pending = self._pending_history_row
        self._pending_history_row = None
        if pending is not None:
            self._load_selected_history(pending)

    def _apply_history(self, request: tuple[str, int, int], value: object) -> None:
        selected = self._selected()
        selected_key = str((selected or {}).get("_bridge_key") or (selected or {}).get("channel") or "")
        if request != self._history_request or not selected or (
            selected_key, int(selected.get("id") or 0)
        ) != request[:2]:
            return
        rows = list(dict(value or {}).get("events") or [])
        assert self._history_table is not None and self._history_status is not None
        self._decision_rows = []
        self._history_table.blockSignals(True)
        self._history_table.setRowCount(len(rows))
        for row_index, event in enumerate(rows):
            values = [
                "—",
                selected.get("channel") or "",
                selected.get("contact_id") or "",
                "NOOP" if str(event.get("event") or "").upper() == "NONE" else event.get("event") or "",
                event.get("old_memory") or "—",
                event.get("new_memory") or "—",
                event.get("reason") or "模型语义决策（历史版本未记录原因）",
                event.get("updated_at") or event.get("created_at") or "",
            ]
            for column, value_item in enumerate(values):
                self._history_table.setItem(row_index, column, _item(value_item))
        self._history_table.blockSignals(False)
        self._history_table.resizeRowsToContents()
        self._update_decision_selection()
        self._history_status.setText(f"共 {len(rows)} 次语义治理事件；可据此审阅冲突合并与删除")

    def _apply_decisions(self, rows: list[dict]) -> None:
        if self._history_table is None or self._history_status is None:
            return
        self._decision_rows = list(rows)
        self._history_table.blockSignals(True)
        self._history_table.setRowCount(len(rows))
        for row_index, event in enumerate(rows):
            values = [
                event.get("channel") or "",
                event.get("contact_id") or "",
                "NOOP" if str(event.get("event") or "").upper() == "NONE" else event.get("event") or "",
                event.get("old_memory") or "—",
                event.get("new_memory") or "—",
                event.get("reason") or "模型语义决策",
                event.get("updated_at") or event.get("created_at") or "",
            ]
            check = _item("")
            check.setFlags(check.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            check.setCheckState(Qt.CheckState.Unchecked)
            check.setData(Qt.ItemDataRole.UserRole, int(event.get("id") or 0))
            check.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._history_table.setItem(row_index, 0, check)
            for column, value_item in enumerate(values, start=1):
                self._history_table.setItem(row_index, column, _item(value_item))
        self._history_table.blockSignals(False)
        self._history_table.resizeRowsToContents()
        self._update_decision_selection()
        self._history_status.setText(
            f"最近 {len(rows)} 次语义治理决策（包含 ADD / UPDATE / DELETE / NOOP）"
        )

    def _decision_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == 0:
            self._update_decision_selection()

    def _checked_decisions(self) -> list[dict]:
        if self._history_table is None or len(self._decision_rows) != self._history_table.rowCount():
            return []
        return [
            row for index, row in enumerate(self._decision_rows)
            if self._history_table.item(index, 0) is not None
            and self._history_table.item(index, 0).checkState() == Qt.CheckState.Checked
        ]

    def _update_decision_selection(self) -> None:
        selected = len(self._checked_decisions())
        if self._decision_selection_status is not None:
            self._decision_selection_status.setText(f"已选 {selected} 条")
        if self._decision_delete is not None:
            self._decision_delete.setEnabled(selected > 0)

    def _set_all_decisions_checked(self, state: Qt.CheckState) -> None:
        if self._history_table is None or len(self._decision_rows) != self._history_table.rowCount():
            return
        self._history_table.blockSignals(True)
        for row in range(self._history_table.rowCount()):
            item = self._history_table.item(row, 0)
            if item is not None:
                item.setCheckState(state)
        self._history_table.blockSignals(False)
        self._update_decision_selection()

    def _select_all_decisions(self) -> None:
        self._set_all_decisions_checked(Qt.CheckState.Checked)

    def _clear_all_decisions(self) -> None:
        self._set_all_decisions_checked(Qt.CheckState.Unchecked)

    def _delete_selected_decisions(self) -> None:
        selected = self._checked_decisions()
        if not selected or self._history_status is None:
            if self._history_status is not None:
                self._history_status.setText("请先勾选要删除的语义治理决策")
            return
        if QMessageBox.question(
            self,
            "删除语义治理决策",
            f"确定删除所选 {len(selected)} 条治理决策记录？此操作只清理审计记录，不会删除长期记忆，且不可撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return

        def remove_selected() -> int:
            grouped: dict[str, list[int]] = {}
            for row in selected:
                key = str(row.get("_bridge_key") or row.get("channel") or "")
                grouped.setdefault(key, []).append(int(row["id"]))
            deleted = 0
            for key, ids in grouped.items():
                result = self._bridge_for_key(key).delete_memory_decisions(ids) or {}
                deleted += int(result.get("deleted") or 0)
            return deleted

        if self._start(
            remove_selected,
            self._decision_delete_finished,
            lambda message: self._history_status.setText(f"治理决策删除失败：{message}"),
        ):
            self._history_status.setText(f"正在删除所选 {len(selected)} 条治理决策…")

    def _decision_delete_finished(self, value: object) -> None:
        if self._history_status is not None:
            self._history_status.setText(f"已删除 {int(value or 0)} 条治理决策，正在刷新…")
        self.refresh()

    def _history_failed(self, request: tuple[str, int, int], message: str) -> None:
        selected = self._selected()
        selected_key = str((selected or {}).get("_bridge_key") or (selected or {}).get("channel") or "")
        if (
            request == self._history_request and selected
            and (selected_key, int(selected.get("id") or 0)) == request[:2]
            and self._history_status is not None
        ):
            self._history_status.setText(f"治理轨迹读取失败：{message}")

    def _values(self) -> dict | None:
        contact, content = self._contact.text().strip(), self._content.text().strip()
        if not contact or not content:
            self._stats.setText("客户 ID 和记忆内容不能为空")
            return None
        editable = {"memory_type": self._edit_type.currentData(), "content": content,
                    "importance": self._importance.value()}
        if self.governance:
            return editable
        editable.update({
            "channel": str(self._channel.currentData() or self._channel.currentText()),
            "contact_id": contact,
        })
        return editable

    def _create(self) -> None:
        values = self._values()
        channel_key = str(self._channel.currentData() or self._channel.currentText())
        bridge = self._bridge_for_key(channel_key)
        if values and self._start(lambda: bridge.create_memory(values), lambda _v: self.refresh(),
                                  lambda m: self._stats.setText(f"新增失败：{m}")):
            self._stats.setText("正在新增记忆…")

    def _save(self) -> None:
        row, values = self._selected(), self._values()
        if not row or not values:
            return
        bridge = self._bridge_for_key(str(row.get("_bridge_key") or row.get("channel") or ""))
        memory_id = int(row["id"])
        if self._start(lambda: bridge.update_memory(memory_id, values),
                                          lambda _v: self.refresh(),
                                          lambda m: self._stats.setText(f"保存失败：{m}")):
            self._stats.setText("正在保存…")

    def _toggle_pin(self) -> None:
        row = self._selected()
        if not row:
            return
        bridge = self._bridge_for_key(str(row.get("_bridge_key") or row.get("channel") or ""))
        memory_id, new_pin = int(row["id"]), not bool(row.get("is_pinned"))
        if self._start(lambda: bridge.update_memory(memory_id, {"is_pinned": new_pin}),
                               lambda _v: self.refresh(), lambda m: self._stats.setText(f"置顶失败：{m}")):
            self._stats.setText("正在更新置顶状态…")

class WorkerTasksPage(_AsyncPage):
    def __init__(self, bridge):
        super().__init__(); self.bridge = bridge; self.setObjectName("OpsPage"); self._rows = []
        root = QVBoxLayout(self); root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(_title("Worker 任务", "查看画像更新、记忆召回和诊断任务的真实状态与耗时"))
        self._status = QLabel("等待加载"); self._status.setObjectName("OpsStatus"); root.addWidget(self._status)
        self._table = _table(["选择", "任务 ID", "类型", "任务", "状态", "耗时(ms)", "结果", "开始时间"])
        self._table.itemChanged.connect(self._item_changed)
        root.addWidget(self._table, 1)
        actions = QHBoxLayout()
        self._select_all = QPushButton("全选"); self._select_all.clicked.connect(self._select_all_rows)
        self._clear_all = QPushButton("取消全选"); self._clear_all.clicked.connect(self._clear_all_rows)
        self._delete_selected = QPushButton("删除"); self._delete_selected.setObjectName("DangerButton")
        self._delete_selected.setEnabled(False); self._delete_selected.clicked.connect(self._delete_rows)
        button = QPushButton("刷新任务"); button.clicked.connect(self.refresh)
        actions.addWidget(self._select_all); actions.addWidget(self._clear_all)
        actions.addWidget(self._delete_selected); actions.addStretch(1); actions.addWidget(button)
        root.addLayout(actions)

    def refresh(self, *_args) -> None:
        self._status.setText("正在加载 Worker 任务…")
        self._start(self.bridge.ops_overview, self._apply, lambda m: self._status.setText(f"加载失败：{m}"))

    def _apply(self, value: object) -> None:
        self._rows = list(dict(value or {}).get("worker_tasks") or [])
        self._status.setText(f"当前后端进程记录 {len(self._rows)} 项任务")
        self._table.blockSignals(True); self._table.setRowCount(len(self._rows))
        for i, row in enumerate(self._rows):
            values = [row.get("id"), row.get("kind"), row.get("label"), row.get("status"),
                      row.get("duration_ms"), row.get("detail"), row.get("started_at")]
            check = _item(""); check.setFlags(check.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            check.setCheckState(Qt.CheckState.Unchecked); self._table.setItem(i, 0, check)
            for j, item in enumerate(values, 1): self._table.setItem(i, j, _item(item))
        self._table.blockSignals(False); self._update_selection()

    def _checked_rows(self) -> list[dict]:
        return [row for index, row in enumerate(self._rows)
                if self._table.item(index, 0) is not None
                and self._table.item(index, 0).checkState() == Qt.CheckState.Checked]

    def _item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == 0: self._update_selection()

    def _update_selection(self) -> None:
        self._delete_selected.setEnabled(bool(self._checked_rows()))

    def _set_all_checked(self, state: Qt.CheckState) -> None:
        self._table.blockSignals(True)
        for index in range(self._table.rowCount()): self._table.item(index, 0).setCheckState(state)
        self._table.blockSignals(False); self._update_selection()

    def _select_all_rows(self) -> None: self._set_all_checked(Qt.CheckState.Checked)
    def _clear_all_rows(self) -> None: self._set_all_checked(Qt.CheckState.Unchecked)

    def _delete_rows(self) -> None:
        rows = self._checked_rows()
        if not rows: return
        if QMessageBox.question(
            self, "删除 Worker 任务记录",
            f"确定删除所选 {len(rows)} 条任务记录？运行中的实际任务不会被取消。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes: return
        ids = [str(row.get("id") or "") for row in rows]
        if self._start(lambda: self.bridge.delete_ops_tasks(ids), lambda _v: self.refresh(),
                       lambda m: self._status.setText(f"删除失败：{m}")):
            self._status.setText(f"正在删除 {len(ids)} 条任务记录…")


class DataBrowserPage(_AsyncPage):
    def __init__(self, bridge):
        super().__init__(); self.bridge = bridge; self.setObjectName("OpsPage"); self._rows = []; self._loaded_dataset = ""
        root = QVBoxLayout(self); root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(_title("数据浏览", "查看并选择删除当前租户的白名单业务数据，不开放任意 SQL"))
        bar = QHBoxLayout(); self._dataset = QComboBox()
        for key, label in (("conversations", "会话"), ("messages", "消息"), ("memories", "长期记忆"), ("knowledge", "知识文档")):
            self._dataset.addItem(label, key)
        self._limit = QSpinBox(); self._limit.setRange(10, 200); self._limit.setValue(100)
        button = QPushButton("加载数据"); button.setObjectName("Primary"); button.clicked.connect(self.refresh)
        bar.addWidget(self._dataset); bar.addWidget(QLabel("最多")); bar.addWidget(self._limit); bar.addWidget(button); bar.addStretch(1)
        root.addLayout(bar)
        self._status = QLabel("等待加载"); self._status.setObjectName("OpsStatus"); root.addWidget(self._status)
        self._table = _table(["选择", "数据"]); self._table.itemChanged.connect(self._item_changed); root.addWidget(self._table, 1)
        actions = QHBoxLayout()
        self._select_all = QPushButton("全选"); self._select_all.clicked.connect(self._select_all_rows)
        self._clear_all = QPushButton("取消全选"); self._clear_all.clicked.connect(self._clear_all_rows)
        self._delete_selected = QPushButton("删除"); self._delete_selected.setObjectName("DangerButton")
        self._delete_selected.setEnabled(False); self._delete_selected.clicked.connect(self._delete_rows)
        actions.addWidget(self._select_all); actions.addWidget(self._clear_all)
        actions.addWidget(self._delete_selected); actions.addStretch(1); root.addLayout(actions)

    def refresh(self, *_args) -> None:
        dataset, limit = str(self._dataset.currentData()), self._limit.value()
        self._status.setText("正在读取租户数据…")
        self._start(lambda: self.bridge.browse_ops_data(dataset, limit), self._apply,
                    lambda m: self._status.setText(f"读取失败：{m}"))

    def _apply(self, value: object) -> None:
        payload = dict(value or {}); columns = list(payload.get("columns") or []); self._rows = list(payload.get("rows") or [])
        self._loaded_dataset = str(payload.get("dataset") or self._dataset.currentData() or "")
        self._table.blockSignals(True); self._table.setColumnCount(len(columns) + 1)
        self._table.setHorizontalHeaderLabels(["选择", *columns]); self._table.setRowCount(len(self._rows))
        for i, row in enumerate(self._rows):
            check = _item(""); check.setFlags(check.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            check.setCheckState(Qt.CheckState.Unchecked); self._table.setItem(i, 0, check)
            for j, key in enumerate(columns, 1): self._table.setItem(i, j, _item(row.get(key)))
        self._table.blockSignals(False); self._update_selection()
        self._table.horizontalHeader().setStretchLastSection(True)
        self._status.setText(f"{self._loaded_dataset} · 已加载 {len(self._rows)} 条（租户隔离）")

    def _checked_rows(self) -> list[dict]:
        return [row for index, row in enumerate(self._rows)
                if self._table.item(index, 0) is not None
                and self._table.item(index, 0).checkState() == Qt.CheckState.Checked]

    def _item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == 0: self._update_selection()

    def _update_selection(self) -> None:
        self._delete_selected.setEnabled(bool(self._checked_rows()))

    def _set_all_checked(self, state: Qt.CheckState) -> None:
        self._table.blockSignals(True)
        for index in range(self._table.rowCount()): self._table.item(index, 0).setCheckState(state)
        self._table.blockSignals(False); self._update_selection()

    def _select_all_rows(self) -> None: self._set_all_checked(Qt.CheckState.Checked)
    def _clear_all_rows(self) -> None: self._set_all_checked(Qt.CheckState.Unchecked)

    def _delete_rows(self) -> None:
        rows = self._checked_rows()
        if not rows: return
        warning = "删除会话时会同时删除其消息和成交记录；删除知识文档会同步删除向量片段。"
        if QMessageBox.question(
            self, "删除数据",
            f"确定删除所选 {len(rows)} 条 {self._loaded_dataset} 数据？{warning}此操作不可撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes: return
        ids = [int(row.get("id") or 0) for row in rows]
        if self._start(
            lambda: self.bridge.delete_ops_data(self._loaded_dataset, ids),
            lambda _v: self.refresh(), lambda m: self._status.setText(f"删除失败：{m}"),
        ):
            self._status.setText(f"正在删除所选 {len(ids)} 条数据…")


class FunctionalTestsPage(_AsyncPage):
    def __init__(self, bridge, *, benchmark_page=None):
        super().__init__(); self.bridge = bridge; self.setObjectName("OpsPage")
        self._benchmark_page = benchmark_page
        self._benchmark_loaded = False
        root = QVBoxLayout(self); root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(_title(
            "功能测试",
            "系统健康检查，不代表真实的长期记忆",
        ))
        tabs = QTabWidget(); tabs.setObjectName("FunctionalTestTabs")
        system = QWidget(); system_layout = QVBoxLayout(system); system_layout.setContentsMargins(8, 12, 8, 8)
        self._status = QLabel("尚未运行"); self._status.setObjectName("OpsStatus"); system_layout.addWidget(self._status)
        self._table = _table(["检查项", "状态", "详情"]); system_layout.addWidget(self._table, 1)
        button = QPushButton("运行系统功能测试"); button.setObjectName("Primary"); button.clicked.connect(self.refresh); system_layout.addWidget(button)
        tabs.addTab(system, "系统检查")
        if benchmark_page is not None:
            tabs.addTab(benchmark_page, "LoCoMo 公开集评测")
        tabs.currentChanged.connect(self._tab_changed)
        root.addWidget(tabs, 1)

    def _tab_changed(self, index: int) -> None:
        if self._benchmark_page is None or self._benchmark_loaded:
            return
        tabs = self.findChild(QTabWidget, "FunctionalTestTabs")
        if tabs is not None and tabs.widget(index) is self._benchmark_page:
            self._benchmark_loaded = True
            self._benchmark_page.refresh()

    def refresh(self, *_args) -> None:
        self._status.setText("正在执行安全检查…")
        self._start(self.bridge.run_ops_tests, self._apply, lambda m: self._status.setText(f"测试失败：{m}"))

    def _apply(self, value: object) -> None:
        payload = dict(value or {}); rows = list(payload.get("checks") or [])
        self._table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, key in enumerate(("name", "status", "detail")): self._table.setItem(i, j, _item(row.get(key)))
        self._status.setText("全部核心检查通过" if payload.get("passed") else "存在需要处理的检查项")


class ModelGatewayPage(_AsyncPage):
    def __init__(self, bridge):
        super().__init__(); self.bridge = bridge; self.setObjectName("OpsPage")
        root = QVBoxLayout(self); root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(_title("模型网关", "查看当前对话与向量模型路由；密钥始终保持隐藏"))
        self._cards: dict[str, QLabel] = {}
        for key, label in (("provider", "提供商"), ("chat_model", "对话模型"), ("embedding_model", "向量模型"),
                           ("embedding_dimension", "向量维度"), ("thinking", "深度思考"), ("mode", "运行模式")):
            card = QFrame(); card.setObjectName("OpsCard"); lay = QVBoxLayout(card)
            caption = QLabel(label); caption.setObjectName("OpsMuted"); value = QLabel("—"); value.setObjectName("OpsCardValue")
            lay.addWidget(caption); lay.addWidget(value); root.addWidget(card); self._cards[key] = value
        self._status = QLabel("等待加载"); self._status.setObjectName("OpsStatus"); root.addWidget(self._status); root.addStretch(1)
        button = QPushButton("刷新网关状态"); button.clicked.connect(self.refresh); root.addWidget(button)

    def refresh(self, *_args) -> None:
        self._start(self.bridge.ops_overview, self._apply, lambda m: self._status.setText(f"加载失败：{m}"))

    def _apply(self, value: object) -> None:
        gateway = dict(dict(value or {}).get("model_gateway") or {})
        for key, label in self._cards.items():
            raw = gateway.get(key, "—"); label.setText("开启" if raw is True else "关闭" if raw is False else str(raw))
        self._status.setText("模型网关配置完整" if gateway.get("configured") else "模型网关配置不完整")


def _title(title: str, subtitle: str) -> QFrame:
    frame = QFrame(); frame.setObjectName("OpsHeading"); lay = QVBoxLayout(frame); lay.setContentsMargins(0, 0, 0, 6)
    heading = QLabel(title); heading.setObjectName("OpsTitle"); muted = QLabel(subtitle); muted.setObjectName("OpsMuted")
    lay.addWidget(heading); lay.addWidget(muted); return frame
