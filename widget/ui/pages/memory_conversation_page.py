"""Real WeChat conversation ingestion page for the Mem0 memory pipeline."""
from __future__ import annotations

import hashlib
import threading
from datetime import datetime
from time import monotonic
from typing import Any

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QHeaderView, QLabel, QListWidget,
    QListWidgetItem, QProgressBar, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QMessageBox,
)

from widget.ui.pages.operations_page import _AsyncPage
from widget.wechat_accounts import is_group_account
from widget.live_conversation import event_to_history_message, merge_live_messages


def _display_time(timestamp: int) -> str:
    if timestamp <= 0:
        return "—"
    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return str(timestamp)


def normalize_history_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert local hook history into the small, text-only contract accepted by Mem0."""
    out: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        text = str(message.get("text") or "").strip()
        if not text:
            continue
        # 群聊以群 ID 作为独立记忆作用域，并把真实发言人保留在正文中，避免把多个
        # 群成员误当成同一个“好友”。私聊正文保持原样。
        if message.get("is_group") and not message.get("is_self"):
            sender = str(message.get("sender_name") or message.get("sender_id") or "群成员").strip()
            text = f"{sender}：{text}"
        timestamp = int(message.get("ts") or 0)
        raw_key = message.get("local_id") or message.get("id") or message.get("message_id")
        if raw_key is None:
            digest = hashlib.sha256(
                f"{timestamp}\0{bool(message.get('is_self'))}\0"
                f"{message.get('sender_id') or ''}\0{message.get('type') or ''}\0{text}".encode("utf-8")
            ).hexdigest()[:32]
            key = digest
        else:
            key = str(raw_key)
        out.append({
            "key": key,
            "role": "assistant" if message.get("is_self") else "user",
            "content": text[:4000],
            "timestamp": timestamp,
        })
    return out


class MemoryConversationPage(_AsyncPage):
    _ingest_progress = Signal(int, int, str)
    _names_ready = Signal(object)

    def __init__(self, bridge, *, adapter=None, channels=None, adapter_for=None,
                 bridge_for=None, default_channel: str = "", labels=None):
        super().__init__()
        self.setObjectName("OpsPage")
        self.bridge = bridge
        self._fallback_adapter = adapter
        self._adapter_for = adapter_for
        self._bridge_for = bridge_for
        self._channels = list(channels or [])
        self._labels = dict(labels or {})
        keys = [key for key, _label in self._channels]
        self._channel_key = default_channel if default_channel in keys else (
            keys[0] if keys else str(getattr(adapter, "channel", "wechat_personal"))
        )
        self._sessions: list[dict] = []
        self._messages: list[dict] = []
        self._session_cache: dict[str, list[dict]] = {}
        self._message_cache: dict[tuple[str, str, int], list[dict]] = {}
        self._live_messages: dict[tuple[str, str], list[dict]] = {}
        self._request_generation = 0
        self._requested_sessions: tuple[str, int] | None = None
        self._requested_sessions_preserve = False
        self._requested_identity: tuple[str, str, int] | None = None
        self._loaded_identity: tuple[str, str] | None = None
        self._sessions_refresh_pending = False
        self._sessions_refresh_pending_preserve = False
        self._progress_total = 0
        self._progress_done = 0
        self._progress_stage = ""
        self._ingest_started_at = 0.0
        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(1000)
        self._progress_timer.timeout.connect(self._refresh_progress_elapsed)
        self._ingest_progress.connect(self._on_ingest_progress)
        self._names_ready.connect(self._apply_names)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        usage = QFrame()
        usage.setObjectName("OpsEditor")
        usage_layout = QVBoxLayout(usage)
        usage_layout.setContentsMargins(12, 9, 12, 9)
        usage_layout.setSpacing(4)
        usage_title = QLabel("长期记忆如何参与 AI 客服回复")
        usage_title.setObjectName("OpsCardValue")
        usage_layout.addWidget(usage_title)
        self._memory_usage = QLabel(
            "真实微信对话→提炼长期记忆→客户下次发消息时按当前话题语义召回→只把相关记忆交给客服模型→"
            "生成有连续感的客服回复。事实：稳定背景；偏好：沟通或选择倾向；需求：正在推进的目标；"
            "承诺：客户或客服明确约定的后续；备注：人工确认的补充信息。不相关记忆不会整库注入。"
            "当前自动注入范围是非业务闲聊或情绪支持；涉及产品、售后、投诉等使用知识库，缺少依据就转人工。"
        )
        self._memory_usage.setObjectName("OpsMuted")
        self._memory_usage.setWordWrap(True)
        usage_layout.addWidget(self._memory_usage)
        root.addWidget(usage)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("渠道"))
        self._channel = QComboBox()
        if self._channels:
            for key, label in self._channels:
                self._channel.addItem(label, key)
        else:
            self._channel.addItem(self._labels.get(self._channel_key, self._channel_key), self._channel_key)
        index = self._channel.findData(self._channel_key)
        if index >= 0:
            self._channel.setCurrentIndex(index)
        self._channel.currentIndexChanged.connect(self._channel_changed)
        controls.addWidget(self._channel)
        controls.addWidget(QLabel("当前会话最近条数"))
        self._limit = QSpinBox()
        self._limit.setRange(1, 500)
        self._limit.setSingleStep(1)
        self._limit.setValue(100)
        self._limit.valueChanged.connect(self._limit_changed)
        controls.addWidget(self._limit)
        controls.addStretch(1)
        refresh = QPushButton("刷新真实会话")
        refresh.setObjectName("Ghost")
        refresh.clicked.connect(self.refresh)
        controls.addWidget(refresh)
        root.addLayout(controls)

        body = QHBoxLayout()
        self._session_list = QListWidget()
        self._session_list.setObjectName("MemoryConversationList")
        self._session_list.currentRowChanged.connect(self._load_selected)
        body.addWidget(self._session_list, 1)

        right = QVBoxLayout()
        title_row = QHBoxLayout()
        self._selected_title = QLabel("请选择一位好友或群聊")
        self._selected_title.setObjectName("OpsCardValue")
        title_row.addWidget(self._selected_title, 1)
        self._select_all = QPushButton("全选")
        self._select_all.setObjectName("Ghost")
        self._select_all.clicked.connect(self._select_all_messages)
        self._select_all.setEnabled(False)
        title_row.addWidget(self._select_all)
        self._clear_all = QPushButton("取消全选")
        self._clear_all.setObjectName("Ghost")
        self._clear_all.clicked.connect(self._clear_all_messages)
        self._clear_all.setEnabled(False)
        title_row.addWidget(self._clear_all)
        right.addLayout(title_row)
        self._table = QTableWidget(0, 4)
        self._table.setObjectName("OpsTable")
        self._table.setHorizontalHeaderLabels(["选择", "方向", "时间", "聊天内容"])
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._table.verticalHeader().setVisible(False)
        self._table.setWordWrap(True)
        self._table.itemChanged.connect(self._selection_changed)
        right.addWidget(self._table, 1)
        body.addLayout(right, 3)
        root.addLayout(body, 1)

        footer = QFrame()
        footer.setObjectName("OpsEditor")
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(12, 9, 12, 9)
        footer_layout.setSpacing(6)
        self._progress_bar = QProgressBar()
        self._progress_bar.setObjectName("MemoryIngestProgress")
        self._progress_bar.setRange(0, 1)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(True)
        self._progress_bar.hide()
        footer_layout.addWidget(self._progress_bar)
        self._progress_elapsed = QLabel("")
        self._progress_elapsed.setObjectName("OpsMuted")
        self._progress_elapsed.hide()
        footer_layout.addWidget(self._progress_elapsed)
        footer_row = QHBoxLayout()
        self._status = QLabel("尚未读取聊天")
        self._status.setObjectName("OpsStatus")
        self._status.setWordWrap(True)
        footer_row.addWidget(self._status, 1)
        self._ingest = QPushButton("提取长期记忆")
        self._ingest.setObjectName("Primary")
        self._ingest.clicked.connect(self._ingest_selected)
        self._ingest.setEnabled(False)
        footer_row.addWidget(self._ingest)
        footer_layout.addLayout(footer_row)
        root.addWidget(footer)

    def _adapter(self):
        if self._adapter_for is not None:
            return self._adapter_for(self._channel_key)
        return self._fallback_adapter

    def _bridge_for_key(self, channel_key: str):
        return self._bridge_for(channel_key) if self._bridge_for is not None else self.bridge

    def _channel_changed(self, _index: int) -> None:
        self._channel_key = str(self._channel.currentData() or "")
        self._request_generation += 1
        self._requested_sessions = None
        self._requested_identity = None
        self._loaded_identity = None
        self._sessions = []
        self._messages = []
        self._session_list.blockSignals(True)
        self._session_list.clear()
        self._session_list.blockSignals(False)
        self._table.setRowCount(0)
        self._select_all.setEnabled(False)
        self._clear_all.setEnabled(False)
        self._ingest.setEnabled(False)
        self.refresh()

    def _limit_changed(self, _value: int) -> None:
        row = self._session_list.currentRow()
        if 0 <= row < len(self._sessions):
            self._load_selected(row)

    def activate(self) -> None:
        """返回本页时保留左右两栏快照，只在后台检查会话列表变化。"""
        self._refresh_sessions(preserve_snapshot=bool(self._sessions))

    def seed_snapshot(self, snapshot: dict) -> None:
        """先展示会话中心已经读到的同渠道快照，随后 activate() 再后台校准。"""
        if str(snapshot.get("channel") or "") != self._channel_key:
            return
        sessions = list(snapshot.get("sessions") or [])
        names = dict(snapshot.get("names") or {})
        if sessions:
            self._apply_sessions((sessions, names), preserve_snapshot=True, load_selected=False)
        contact_id = str(snapshot.get("contact_id") or "")
        messages = list(snapshot.get("messages") or [])
        if not contact_id or not messages:
            return
        row = next((index for index, session in enumerate(self._sessions)
                    if session.get("contact_id") == contact_id), -1)
        if row >= 0:
            self._session_list.blockSignals(True)
            self._session_list.setCurrentRow(row)
            self._session_list.blockSignals(False)
        limit = self._limit.value()
        visible = messages[-limit:]
        self._message_cache[(self._channel_key, contact_id, limit)] = visible
        self._loaded_identity = (self._channel_key, contact_id)
        self._apply_messages(visible)
        self._status.setText("已立即显示会话中心快照；正在后台校准本机聊天记录")

    def refresh(self, *_args) -> None:
        """显式刷新也保留可见快照，结果就绪后再原位替换。"""
        self._refresh_sessions(preserve_snapshot=bool(self._sessions))

    def _refresh_sessions(self, *, preserve_snapshot: bool) -> None:
        adapter = self._adapter()
        if adapter is None:
            self._status.setText("当前渠道未连接，无法读取真实聊天记录")
            return
        if self._task is not None:
            self._sessions_refresh_pending = True
            self._sessions_refresh_pending_preserve = preserve_snapshot
            self._status.setText("正在完成上一次任务，随后会自动刷新当前渠道…")
            return
        self._sessions_refresh_pending = False
        self._sessions_refresh_pending_preserve = False
        self._requested_sessions_preserve = preserve_snapshot
        if preserve_snapshot:
            self._status.setText("正在后台刷新本机微信会话，继续显示当前内容…")
        else:
            self._status.setText("正在读取本机微信会话…")
            self._requested_identity = None
            self._loaded_identity = None
            self._messages = []
            self._table.setRowCount(0)
            self._ingest.setEnabled(False)
            self._select_all.setEnabled(False)
            self._clear_all.setEnabled(False)
            self._session_list.setEnabled(False)
            self._limit.setEnabled(False)
            self._table.setEnabled(False)
        self._request_generation += 1
        token = (self._channel_key, self._request_generation)
        self._requested_sessions = token

        def load():
            sessions = list(adapter.list_sessions() or [])
            # 联系人库可能比 session.db 慢数秒；首屏先交付会话，名字随后独立补齐。
            return sessions, {}

        started = self._start(
            load,
            lambda value, request=token: self._sessions_ready(request, value),
            lambda message, request=token: self._sessions_failed(request, message),
        )
        if not started:
            self._sessions_refresh_pending = True
            self._status.setText("正在完成上一次读取，随后会自动刷新当前渠道…")

    def _resume_pending_sessions_refresh(self) -> bool:
        if not self._sessions_refresh_pending:
            return False
        preserve_snapshot = self._sessions_refresh_pending_preserve
        self._sessions_refresh_pending = False
        self._sessions_refresh_pending_preserve = False
        self._refresh_sessions(preserve_snapshot=preserve_snapshot)
        return True

    def _sessions_ready(self, token: tuple[str, int], value: object) -> None:
        if self._resume_pending_sessions_refresh():
            return
        if token != self._requested_sessions:
            self._refresh_sessions(preserve_snapshot=bool(self._sessions))
            return
        self._session_list.setEnabled(True)
        self._limit.setEnabled(True)
        self._table.setEnabled(True)
        self._apply_sessions(value, preserve_snapshot=self._requested_sessions_preserve)
        self._start_names_refresh(token)

    def _sessions_failed(self, token: tuple[str, int], message: str) -> None:
        if self._resume_pending_sessions_refresh():
            return
        if token != self._requested_sessions:
            self._refresh_sessions(preserve_snapshot=bool(self._sessions))
            return
        self._session_list.setEnabled(True)
        self._limit.setEnabled(True)
        self._table.setEnabled(True)
        if self._requested_sessions_preserve and self._sessions:
            self._status.setText(f"后台刷新暂未完成，继续显示原会话：{message}")
        else:
            self._status.setText(f"读取会话失败：{message}")

    def _apply_sessions(self, value: object, *, preserve_snapshot: bool = False,
                        load_selected: bool = True) -> None:
        sessions, names = value if isinstance(value, tuple) else ([], {})
        normalized_sessions = []
        for row in sessions:
            contact_id = str(row.get("wxid") or row.get("contact") or "")
            if not contact_id:
                continue
            name = str(row.get("name") or row.get("display_name")
                       or names.get(contact_id) or contact_id)
            is_group = is_group_account(contact_id)
            normalized = dict(
                row, contact_id=contact_id, display_name=name, is_group=is_group,
            )
            normalized_sessions.append(normalized)

        known_contacts = {str(row.get("contact_id") or "") for row in normalized_sessions}
        for (event_channel, contact_id), live in self._live_messages.items():
            if event_channel != self._channel_key or not live or contact_id in known_contacts:
                continue
            latest = live[-1]
            normalized_sessions.append({
                "wxid": contact_id,
                "contact_id": contact_id,
                "display_name": contact_id,
                "is_group": is_group_account(contact_id),
                "summary": str(latest.get("text") or ""),
                "ts": int(latest.get("ts") or 0),
            })
        normalized_sessions.sort(key=lambda row: int(row.get("ts") or 0), reverse=True)

        # 后台激活刷新偶尔会遇到 hook 的瞬时空结果；它不能推翻已经显示的非空快照。
        if preserve_snapshot and self._sessions and not normalized_sessions:
            self._status.setText("本次后台刷新暂未读到会话，继续显示原有内容")
            return

        selected_contact = ""
        current = self._session_list.currentRow()
        if 0 <= current < len(self._sessions):
            selected_contact = str(self._sessions[current].get("contact_id") or "")
        if preserve_snapshot and self._loaded_identity:
            selected_contact = self._loaded_identity[1]

        self._sessions = normalized_sessions
        self._session_cache[self._channel_key] = [dict(row) for row in normalized_sessions]
        self._session_list.blockSignals(True)
        self._session_list.clear()
        selected_row = -1
        for row_index, normalized in enumerate(self._sessions):
            contact_id = str(normalized["contact_id"])
            name = str(normalized["display_name"])
            is_group = bool(normalized["is_group"])
            prefix = "[群聊] " if is_group else ""
            item = QListWidgetItem(
                f"{prefix}{name}\n{str(normalized.get('summary') or normalized.get('last') or '')[:48]}"
            )
            item.setData(Qt.ItemDataRole.UserRole, normalized)
            self._session_list.addItem(item)
            if contact_id == selected_contact:
                selected_row = row_index
        if self._sessions:
            if selected_row < 0:
                selected_row = 0
            self._session_list.setCurrentRow(selected_row)
        self._session_list.blockSignals(False)
        self._status.setText(f"已读取 {len(self._sessions)} 个真实会话；请选择好友或群聊后审阅内容")
        if not self._sessions:
            self._load_selected(-1)
        elif load_selected and not (
            preserve_snapshot
            and self._loaded_identity == (self._channel_key, selected_contact)
            and self._table.rowCount() > 0
        ):
            self._load_selected(selected_row)

    def _start_names_refresh(self, token: tuple[str, int]) -> None:
        adapter = self._adapter()
        ids = [str(row.get("contact_id") or "") for row in self._sessions]
        names_fn = getattr(adapter, "display_names", None) if adapter is not None else None
        if not ids or not callable(names_fn):
            return

        def worker() -> None:
            try:
                names = dict(names_fn(ids) or {})
            except Exception:
                names = {}
            try:
                self._names_ready.emit((token, names))
            except RuntimeError:
                return

        threading.Thread(target=worker, daemon=True, name="memory-conversation-names").start()

    def _apply_names(self, payload: object) -> None:
        token, names = payload if isinstance(payload, tuple) else (None, {})
        if token != self._requested_sessions or not isinstance(names, dict):
            return
        for index, session in enumerate(self._sessions):
            contact_id = str(session.get("contact_id") or "")
            name = str(names.get(contact_id) or session.get("display_name") or contact_id)
            session["display_name"] = name
            item = self._session_list.item(index)
            if item is not None:
                prefix = "[群聊] " if session.get("is_group") else ""
                summary = str(session.get("summary") or session.get("last") or "")[:48]
                item.setText(f"{prefix}{name}\n{summary}")
        self._session_cache[self._channel_key] = [dict(row) for row in self._sessions]

    def _load_selected(self, row_index: int) -> None:
        if not (0 <= row_index < len(self._sessions)):
            self._requested_identity = None
            self._loaded_identity = None
            self._messages = []
            self._table.setRowCount(0)
            self._select_all.setEnabled(False)
            self._clear_all.setEnabled(False)
            self._ingest.setEnabled(False)
            return
        session = self._sessions[row_index]
        adapter = self._adapter()
        if adapter is None:
            return
        self._request_generation += 1
        identity = (self._channel_key, session["contact_id"], self._request_generation)
        self._requested_identity = identity
        limit = self._limit.value()
        cache_key = (self._channel_key, str(session["contact_id"]), limit)
        cached = self._message_cache.get(cache_key)
        if cached is not None:
            self._loaded_identity = identity[:2]
            self._apply_messages(cached)
        else:
            self._loaded_identity = None
            self._messages = []
            self._table.setRowCount(0)
            self._select_all.setEnabled(False)
            self._clear_all.setEnabled(False)
            self._ingest.setEnabled(False)
        scope_label = "群聊" if session.get("is_group") else "好友"
        self._selected_title.setText(f"与 {session['display_name']} 的聊天（{scope_label}记忆）")
        self._status.setText(
            f"正在后台刷新该{scope_label}的真实聊天…" if cached is not None
            else f"正在读取该{scope_label}的真实聊天…"
        )
        contact_id = str(session["contact_id"])
        started = self._start(
            lambda: adapter.read_conversation(contact_id, limit),
            lambda value, token=identity: self._messages_ready(token, value),
            lambda message, token=identity: self._messages_failed(token, message),
        )

        if not started:
            self._status.setText("正在完成上一次读取，随后会自动加载当前好友…")

    def _messages_ready(self, identity: tuple[str, str, int], value: object) -> None:
        if self._resume_pending_sessions_refresh():
            return
        if identity != self._requested_identity:
            self._retry_current_selection()
            return
        self._loaded_identity = identity[:2]
        cache_key = (identity[0], identity[1], self._limit.value())
        merged = merge_live_messages(
            list(value or []), self._live_messages.get((identity[0], identity[1]), []),
            limit=self._limit.value(),
        )
        self._message_cache[cache_key] = merged
        self._apply_messages(merged)

    def apply_live_event(self, event: dict) -> None:
        """实时收发同时预热记忆对话页，不等再次查询本机数据库。"""
        channel = str(event.get("channel") or "")
        contact_id = str(event.get("contact_id") or "")
        if not channel or not contact_id:
            return
        message = event_to_history_message(event)
        live = self._live_messages.setdefault((channel, contact_id), [])
        event_id = str(message.get("live_event_id") or "")
        if not any(str(item.get("live_event_id") or "") == event_id for item in live):
            live.append(message)
            del live[:-50]
        limit = self._limit.value()
        cache_key = (channel, contact_id, limit)
        cached_messages = self._message_cache.get(cache_key, [])
        self._message_cache[cache_key] = merge_live_messages(cached_messages, live, limit=limit)
        sessions = [dict(row) for row in self._session_cache.get(channel, [])]
        existing = next((row for row in sessions if row.get("contact_id") == contact_id), None)
        if existing is None:
            existing = {
                "wxid": contact_id, "contact_id": contact_id, "display_name": contact_id,
                "is_group": bool(event.get("is_group", False)),
            }
        existing["summary"] = str(event.get("text") or "")
        existing["ts"] = int(event.get("timestamp") or 0)
        sessions = [existing] + [row for row in sessions if row.get("contact_id") != contact_id]
        self._session_cache[channel] = sessions
        if channel != self._channel_key:
            return
        selected_contact = self._loaded_identity[1] if self._loaded_identity else ""
        self._apply_sessions((sessions, {}), preserve_snapshot=True, load_selected=False)
        if selected_contact == contact_id:
            self._loaded_identity = (channel, contact_id)
            self._apply_messages(self._message_cache[cache_key])

    def _messages_failed(self, identity: tuple[str, str, int], message: str) -> None:
        if self._resume_pending_sessions_refresh():
            return
        if identity != self._requested_identity:
            self._retry_current_selection()
            return
        self._status.setText(f"读取聊天失败：{message}")

    def _retry_current_selection(self) -> None:
        row = self._session_list.currentRow()
        if 0 <= row < len(self._sessions) and self._task is None:
            self._load_selected(row)

    def _apply_messages(self, value: object) -> None:
        self._messages = list(value or [])
        normalized = normalize_history_messages(self._messages)
        self._table.blockSignals(True)
        self._table.setRowCount(len(normalized))
        current = self._session_list.currentRow()
        is_group = (
            0 <= current < len(self._sessions) and bool(self._sessions[current].get("is_group"))
        )
        for row_index, row in enumerate(normalized):
            selected = QTableWidgetItem("")
            selected.setFlags(
                (selected.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                & ~Qt.ItemFlag.ItemIsEditable
            )
            selected.setCheckState(Qt.CheckState.Unchecked)
            selected.setData(Qt.ItemDataRole.UserRole, row)
            self._table.setItem(row_index, 0, selected)
            values = [
                "我" if row["role"] == "assistant" else ("群成员" if is_group else "好友"),
                _display_time(row["timestamp"]), row["content"],
            ]
            for column, value in enumerate(values, start=1):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(row_index, column, item)
        self._table.blockSignals(False)
        self._table.resizeRowsToContents()
        self._select_all.setEnabled(bool(normalized))
        self._clear_all.setEnabled(bool(normalized))
        self._update_selection_status()

    def _selected_messages(self) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            if item is None or item.checkState() != Qt.CheckState.Checked:
                continue
            value = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(value, dict):
                selected.append(dict(value))
        return selected

    def _set_all_messages_checked(self, checked: bool) -> None:
        self._table.blockSignals(True)
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            if item is not None:
                item.setCheckState(state)
        self._table.blockSignals(False)
        self._update_selection_status()

    def _select_all_messages(self) -> None:
        self._set_all_messages_checked(True)

    def _clear_all_messages(self) -> None:
        self._set_all_messages_checked(False)

    def _selection_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == 0:
            self._update_selection_status()

    def _update_selection_status(self) -> None:
        total = self._table.rowCount()
        selected = len(self._selected_messages())
        self._ingest.setEnabled(selected > 0 and self._loaded_identity is not None)
        self._status.setText(
            f"已加载 {total} 条可分析文本，已选择 {selected} 条；只有勾选内容会交给 Mem0 提炼"
        )

    @staticmethod
    def _elapsed_text(seconds: int) -> str:
        minutes, seconds = divmod(max(0, int(seconds)), 60)
        return f"{minutes:02d}:{seconds:02d}"

    def _begin_ingest_progress(self, total: int) -> None:
        self._progress_total = max(1, int(total))
        self._progress_done = 0
        self._progress_stage = "准备提交第 1 批"
        self._ingest_started_at = monotonic()
        self._progress_bar.setRange(0, self._progress_total)
        self._progress_bar.setValue(0)
        self._progress_bar.setFormat(f"0 / {self._progress_total} 条 · 准备中")
        self._progress_bar.show()
        self._progress_elapsed.show()
        self._refresh_progress_elapsed()
        self._progress_timer.start()

    def _on_ingest_progress(self, done: int, total: int, stage: str) -> None:
        self._progress_total = max(1, int(total))
        self._progress_done = min(max(0, int(done)), self._progress_total)
        self._progress_stage = str(stage)
        self._progress_bar.setRange(0, self._progress_total)
        self._progress_bar.setValue(self._progress_done)
        self._progress_bar.setFormat(
            f"{self._progress_done} / {self._progress_total} 条 · "
            f"{round(self._progress_done * 100 / self._progress_total)}%"
        )
        self._status.setText(self._progress_stage)
        self._refresh_progress_elapsed()

    def _refresh_progress_elapsed(self) -> None:
        elapsed = int(monotonic() - self._ingest_started_at) if self._ingest_started_at else 0
        self._progress_elapsed.setText(
            f"已耗时 {self._elapsed_text(elapsed)} · {self._progress_stage}。"
            "每批最多 40 条；模型速度不固定，单批请求超时上限为 3 分钟。"
        )

    def _finish_ingest_progress(self, *, completed: bool) -> None:
        self._progress_timer.stop()
        elapsed = int(monotonic() - self._ingest_started_at) if self._ingest_started_at else 0
        if completed:
            self._progress_done = self._progress_total
            self._progress_bar.setValue(self._progress_total)
            self._progress_bar.setFormat(
                f"完成 · {self._progress_total} / {self._progress_total} 条"
            )
            result = "完成"
        else:
            self._progress_bar.setFormat(
                f"中止 · {self._progress_done} / {self._progress_total} 条"
            )
            result = "未完成"
        self._progress_elapsed.setText(
            f"{result} · 总耗时 {self._elapsed_text(elapsed)} · "
            "已完成批次不会重复提取，修正问题后可安全重试未完成部分。"
        )

    def _ingest_selected(self) -> None:
        row_index = self._session_list.currentRow()
        if not (0 <= row_index < len(self._sessions)):
            return
        session = self._sessions[row_index]
        expected_identity = (self._channel_key, session["contact_id"])
        if self._loaded_identity != expected_identity:
            self._status.setText("当前好友的聊天尚未读取完成，请稍候")
            return
        messages = self._selected_messages()
        if not messages:
            self._status.setText("请先勾选要交给 Mem0 的聊天消息")
            return
        if not self._confirm_ingest(len(messages), str(session["display_name"])):
            return
        channel_key = self._channel_key
        contact_id = str(session["contact_id"])
        display_name = str(session["display_name"])
        bridge = self._bridge_for_key(channel_key)
        self._ingest.setEnabled(False)
        self._channel.setEnabled(False)
        self._limit.setEnabled(False)
        self._session_list.setEnabled(False)
        self._table.setEnabled(False)
        self._select_all.setEnabled(False)
        self._clear_all.setEnabled(False)
        self._begin_ingest_progress(len(messages))
        self._status.setText("Mem0 正在准备第一批长期记忆提取…")
        started = self._start(
            lambda: bridge.ingest_memory_conversation(
                channel_key, contact_id, messages, display_name=display_name,
                on_progress=lambda done, total, stage: self._ingest_progress.emit(
                    done, total, stage
                ),
            ),
            self._ingest_done,
            self._ingest_failed,
        )
        if not started:
            self._channel.setEnabled(True)
            self._limit.setEnabled(True)
            self._session_list.setEnabled(True)
            self._table.setEnabled(True)
            has_messages = self._table.rowCount() > 0
            self._select_all.setEnabled(has_messages)
            self._clear_all.setEnabled(has_messages)
            self._ingest.setEnabled(bool(self._selected_messages()))
            self._finish_ingest_progress(completed=False)
            self._status.setText("上一项读取任务尚未完成，本次提取尚未提交，请稍后重试")

    def _confirm_ingest(self, count: int, display_name: str) -> bool:
        answer = QMessageBox.question(
            self,
            "确认提取长期记忆",
            f"将 {display_name} 已勾选的 {count} 条文本聊天交给本地 Mem0，并调用当前配置的 "
            "DeepSeek / DashScope 模型完成提取与向量化。长期记忆页显示提炼后的事实/偏好，"
            "不会把原聊天逐条伪装成画像，也不会发送微信消息。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _ingest_done(self, value: object) -> None:
        self._finish_ingest_progress(completed=True)
        if self._resume_pending_sessions_refresh():
            return
        payload = dict(value or {})
        self._channel.setEnabled(True)
        self._limit.setEnabled(True)
        self._session_list.setEnabled(True)
        self._table.setEnabled(True)
        has_messages = self._table.rowCount() > 0
        self._select_all.setEnabled(has_messages)
        self._clear_all.setEnabled(has_messages)
        self._ingest.setEnabled(bool(self._selected_messages()))
        skipped = int(payload.get("skipped_messages") or 0)
        dedupe_note = f"，已去重 {skipped} 条" if skipped else ""
        self._status.setText(
            f"完成：本次处理 {payload.get('processed_messages', 0)} 条聊天{dedupe_note}，"
            f"当前形成 {payload.get('memory_count', 0)} 条长期记忆。"
            "重复点击只会处理已勾选且尚未提取过的消息。"
        )

    def _ingest_failed(self, message: str) -> None:
        self._finish_ingest_progress(completed=False)
        if self._resume_pending_sessions_refresh():
            return
        self._channel.setEnabled(True)
        self._limit.setEnabled(True)
        self._session_list.setEnabled(True)
        self._table.setEnabled(True)
        has_messages = self._table.rowCount() > 0
        self._select_all.setEnabled(has_messages)
        self._clear_all.setEnabled(has_messages)
        self._ingest.setEnabled(bool(self._selected_messages()))
        self._status.setText(f"记忆提取失败：{message}")
