"""Deep-blue LoCoMo memory workbench powered by the repository-local Mem0 service."""
from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QSignalBlocker, Qt, QThreadPool, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
    QScrollArea, QSizePolicy, QSpinBox, QSplitter, QVBoxLayout, QWidget,
)


PIPELINE_STAGES = (
    ("record", "记录消息"),
    ("recall", "召回记忆"),
    ("answer", "模型回复"),
    ("save", "保存回复"),
    ("settle", "沉淀记忆"),
)


class _TaskSignals(QObject):
    completed = Signal(object)
    failed = Signal(str)


class _RecallTask(QRunnable):
    def __init__(self, callback: Callable[[], dict[str, Any]]):
        super().__init__()
        self.callback = callback
        self.signals = _TaskSignals()

    @Slot()
    def run(self) -> None:
        try:
            result = self.callback()
        except Exception as exc:  # the GUI must stay alive when a companion service is offline
            self.signals.failed.emit(str(exc))
            return
        self.signals.completed.emit(result)


def _clear_layout(layout: QVBoxLayout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()


class MemoryPage(QWidget):
    def __init__(self, bridge, *, labels: dict[str, str] | None = None):
        super().__init__()
        self.setObjectName("MemoryPage")
        self.bridge = bridge
        self.labels = dict(labels or {})
        self._payload: dict[str, Any] = {}
        self._base_messages: list[dict[str, Any]] = []
        self._transient_messages: list[dict[str, Any]] = []
        self._recall_task: _RecallTask | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)
        root.addLayout(self._build_intro())
        root.addLayout(self._build_stats())
        root.addWidget(self._build_control_bar())
        root.addWidget(self._build_pipeline())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("MemorySplitter")
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_conversation_panel())
        splitter.addWidget(self._build_recall_panel())
        splitter.setStretchFactor(0, 7)
        splitter.setStretchFactor(1, 4)
        splitter.setSizes([760, 440])
        root.addWidget(splitter, 1)

        self._status = QLabel("正在等待加载 LoCoMo 记忆工作台…")
        self._status.setObjectName("MemoryStatus")
        self._status.setWordWrap(True)
        root.addWidget(self._status)

    def _build_intro(self) -> QHBoxLayout:
        row = QHBoxLayout()
        copy = QVBoxLayout()
        copy.setSpacing(2)
        title = QLabel("LoCoMo 公开集评测")
        title.setObjectName("MemoryPageTitle")
        subtitle = QLabel("功能测试 · LoCoMo-10 · Mem0 OSS 混合召回实验台")
        subtitle.setObjectName("MemoryMuted")
        copy.addWidget(title)
        copy.addWidget(subtitle)
        row.addLayout(copy)
        row.addStretch(1)
        badge = QLabel("LOCAL  /  MEM0")
        badge.setObjectName("MemoryEngineBadge")
        row.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)
        return row

    def _build_stats(self) -> QHBoxLayout:
        stats = QHBoxLayout()
        stats.setSpacing(8)
        self._stat_values: dict[str, QLabel] = {}
        for key, label, hint in (
            ("events", "实时事件", "LoCoMo 消息"),
            ("evidence", "证据", "评测证据点"),
            ("worker_tasks", "Worker 任务", "已处理分块"),
            ("long_term_memories", "长期记忆", "当前召回池"),
        ):
            card = QFrame()
            card.setObjectName("MemoryStatCard")
            lay = QVBoxLayout(card)
            lay.setContentsMargins(14, 10, 14, 10)
            lay.setSpacing(0)
            caption = QLabel(label)
            caption.setObjectName("MemoryStatLabel")
            value = QLabel("—")
            value.setObjectName("MemoryStatValue")
            foot = QLabel(hint)
            foot.setObjectName("MemoryStatHint")
            lay.addWidget(caption)
            lay.addWidget(value)
            lay.addWidget(foot)
            self._stat_values[key] = value
            stats.addWidget(card, 1)
        return stats

    def _build_control_bar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("MemoryToolbar")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(8)
        self._case_combo = QComboBox()
        self._case_combo.setMinimumWidth(190)
        self._case_combo.currentIndexChanged.connect(self._case_changed)
        lay.addWidget(self._case_combo)
        self._session_combo = QComboBox()
        self._session_combo.setMinimumWidth(210)
        self._session_combo.currentIndexChanged.connect(self._session_changed)
        lay.addWidget(self._session_combo)
        self._question_combo = QComboBox()
        self._question_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._question_combo.currentIndexChanged.connect(self._question_changed)
        lay.addWidget(self._question_combo, 1)
        self._across_sessions = QCheckBox("跨会话召回")
        self._across_sessions.setChecked(True)
        self._across_sessions.setToolTip("Mem0 按当前 LoCoMo 用户作用域检索全部已沉淀会话")
        lay.addWidget(self._across_sessions)
        amount_label = QLabel("召回")
        amount_label.setObjectName("MemoryMuted")
        lay.addWidget(amount_label)
        self._limit = QSpinBox()
        self._limit.setRange(1, 50)
        self._limit.setValue(8)
        self._limit.setSuffix(" 条")
        lay.addWidget(self._limit)
        refresh = QPushButton("刷新")
        refresh.setObjectName("MemoryGhost")
        refresh.clicked.connect(self.refresh)
        lay.addWidget(refresh)
        return bar

    def _build_pipeline(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("MemoryPipeline")
        lay = QHBoxLayout(frame)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(6)
        self._stage_dots: dict[str, QLabel] = {}
        self._stage_states: dict[str, QLabel] = {}
        for number, (key, label) in enumerate(PIPELINE_STAGES, 1):
            step = QWidget()
            step.setObjectName("MemoryPipelineStep")
            step_lay = QHBoxLayout(step)
            step_lay.setContentsMargins(0, 0, 0, 0)
            step_lay.setSpacing(7)
            dot = QLabel(str(number))
            dot.setObjectName("MemoryStageDotPending")
            dot.setFixedSize(28, 28)
            dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
            labels = QVBoxLayout()
            labels.setSpacing(0)
            name = QLabel(label)
            name.setObjectName("MemoryStageName")
            state = QLabel("待运行")
            state.setObjectName("MemoryStageState")
            labels.addWidget(name)
            labels.addWidget(state)
            step_lay.addWidget(dot)
            step_lay.addLayout(labels)
            lay.addWidget(step, 1)
            self._stage_dots[key] = dot
            self._stage_states[key] = state
        return frame

    def _build_conversation_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("MemoryPanel")
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        header = QFrame()
        header.setObjectName("MemoryPanelHeader")
        header_lay = QVBoxLayout(header)
        header_lay.setContentsMargins(14, 11, 14, 10)
        header_lay.setSpacing(2)
        self._conversation_title = QLabel("LoCoMo 对话")
        self._conversation_title.setObjectName("MemoryPanelTitle")
        self._conversation_meta = QLabel("等待数据")
        self._conversation_meta.setObjectName("MemoryMuted")
        header_lay.addWidget(self._conversation_title)
        header_lay.addWidget(self._conversation_meta)
        lay.addWidget(header)

        self._message_scroll = QScrollArea()
        self._message_scroll.setObjectName("MemoryMessageScroll")
        self._message_scroll.setWidgetResizable(True)
        self._message_host = QWidget()
        self._message_host.setObjectName("MemoryMessageHost")
        self._message_layout = QVBoxLayout(self._message_host)
        self._message_layout.setContentsMargins(14, 14, 14, 14)
        self._message_layout.setSpacing(10)
        self._message_scroll.setWidget(self._message_host)
        lay.addWidget(self._message_scroll, 1)

        composer = QFrame()
        composer.setObjectName("MemoryComposer")
        composer_lay = QVBoxLayout(composer)
        composer_lay.setContentsMargins(12, 10, 12, 10)
        composer_lay.setSpacing(7)
        prompt_label = QLabel("向 Mem0 提问")
        prompt_label.setObjectName("MemoryFieldLabel")
        composer_lay.addWidget(prompt_label)
        self._query = QPlainTextEdit()
        self._query.setObjectName("MemoryQuery")
        self._query.setPlaceholderText("输入 LoCoMo 问题，运行跨会话长期记忆召回…")
        self._query.setMaximumHeight(76)
        composer_lay.addWidget(self._query)
        action_row = QHBoxLayout()
        self._query_count = QLabel("0 / 1000")
        self._query_count.setObjectName("MemoryMuted")
        self._query.textChanged.connect(self._update_query_count)
        action_row.addWidget(self._query_count)
        action_row.addStretch(1)
        self._run_button = QPushButton("运行 Mem0 召回")
        self._run_button.setObjectName("MemoryPrimary")
        self._run_button.clicked.connect(self._run_recall)
        action_row.addWidget(self._run_button)
        composer_lay.addLayout(action_row)
        lay.addWidget(composer)
        return panel

    def _build_recall_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("MemoryPanel")
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        header = QFrame()
        header.setObjectName("MemoryPanelHeader")
        header_lay = QHBoxLayout(header)
        header_lay.setContentsMargins(14, 11, 14, 10)
        copy = QVBoxLayout()
        copy.setSpacing(2)
        self._recall_title = QLabel("本轮召回")
        self._recall_title.setObjectName("MemoryPanelTitle")
        self._recall_meta = QLabel("等待运行")
        self._recall_meta.setObjectName("MemoryMuted")
        copy.addWidget(self._recall_title)
        copy.addWidget(self._recall_meta)
        header_lay.addLayout(copy)
        header_lay.addStretch(1)
        self._result_badge = QLabel("0")
        self._result_badge.setObjectName("MemoryResultBadge")
        self._result_badge.setFixedSize(34, 34)
        self._result_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header_lay.addWidget(self._result_badge)
        lay.addWidget(header)

        scroll = QScrollArea()
        scroll.setObjectName("MemoryRecallScroll")
        scroll.setWidgetResizable(True)
        host = QWidget()
        host.setObjectName("MemoryRecallHost")
        self._recall_layout = QVBoxLayout(host)
        self._recall_layout.setContentsMargins(0, 0, 0, 0)
        self._recall_layout.setSpacing(0)
        scroll.setWidget(host)
        lay.addWidget(scroll, 1)
        return panel

    @staticmethod
    def _set_object_name(widget: QWidget, name: str) -> None:
        widget.setObjectName(name)
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def _set_stage(self, key: str, status: str) -> None:
        dot = self._stage_dots[key]
        if status == "active":
            self._set_object_name(dot, "MemoryStageDotActive")
            self._stage_states[key].setText("运行中")
        elif status == "readonly":
            self._set_object_name(dot, "MemoryStageDotReadonly")
            self._stage_states[key].setText("数据集只读")
        elif status == "complete":
            self._set_object_name(dot, "MemoryStageDotDone")
            self._stage_states[key].setText("完成")
        else:
            self._set_object_name(dot, "MemoryStageDotPending")
            self._stage_states[key].setText("待运行")

    def _set_pipeline(self, active: str | None = None,
                      stages: list[dict[str, Any]] | None = None) -> None:
        resolved = {item.get("key"): item.get("status") for item in (stages or [])}
        for key, _label in PIPELINE_STAGES:
            self._set_stage(key, "active" if key == active else resolved.get(key, "pending"))

    def _case_changed(self, *_args) -> None:
        if self._case_combo.count():
            self._transient_messages.clear()
            self.refresh()

    def _session_changed(self, *_args) -> None:
        if self._session_combo.count():
            self._transient_messages.clear()
            self.refresh()

    def _question_changed(self, *_args) -> None:
        question = self._question_combo.currentData()
        if isinstance(question, dict):
            self._query.setPlainText(question.get("question", ""))
            if self._payload:
                self.refresh()

    def _update_query_count(self) -> None:
        text = self._query.toPlainText()
        if len(text) > 1000:
            blocker = QSignalBlocker(self._query)
            self._query.setPlainText(text[:1000])
            del blocker
            text = text[:1000]
        self._query_count.setText(f"{len(text)} / 1000")

    def refresh(self, *_args) -> None:
        case_index = int(self._case_combo.currentData() or 0)
        session_index = int(self._session_combo.currentData() or 1)
        question_data = self._question_combo.currentData()
        question_index = int(question_data.get("index", 0)) if isinstance(question_data, dict) else 0
        try:
            payload = self.bridge.memory_workbench(
                case_index=case_index,
                session_index=session_index,
                question_index=question_index,
                preview_limit=self._limit.value(),
            )
        except Exception as exc:
            self._set_status(f"记忆工作台暂不可用：{exc}", error=True)
            return
        self._payload = dict(payload or {})
        self._apply_payload()

    def _apply_payload(self) -> None:
        payload = self._payload
        blockers = [
            QSignalBlocker(self._case_combo),
            QSignalBlocker(self._session_combo),
            QSignalBlocker(self._question_combo),
        ]
        self._case_combo.clear()
        for item in payload.get("cases") or []:
            self._case_combo.addItem(item.get("label", ""), int(item.get("index", 0)))
        dataset = payload.get("dataset") or {}
        index = self._case_combo.findData(int(dataset.get("case_index", 0)))
        self._case_combo.setCurrentIndex(max(0, index))

        self._session_combo.clear()
        for session in payload.get("sessions") or []:
            label = f"会话 {session.get('index')} · {session.get('date_time', '')} · {session.get('message_count', 0)} 条"
            self._session_combo.addItem(label, int(session.get("index", 1)))
        index = self._session_combo.findData(int(payload.get("selected_session", 1)))
        self._session_combo.setCurrentIndex(max(0, index))

        self._question_combo.clear()
        for item in payload.get("questions") or []:
            label = f"Q{int(item.get('index', 0)) + 1} · {item.get('question', '')}"
            self._question_combo.addItem(label, item)
        selected_question = int(payload.get("selected_question", 0))
        if 0 <= selected_question < self._question_combo.count():
            self._question_combo.setCurrentIndex(selected_question)
        del blockers

        runtime = payload.get("runtime_overview") or {}
        stats = runtime.get("stats") or payload.get("stats") or {}
        for key, label in self._stat_values.items():
            label.setText(str(stats.get(key, 0)))
        speakers = payload.get("speakers") or {}
        messages = list(payload.get("messages") or [])
        self._base_messages = messages
        self._conversation_title.setText(f"{speakers.get('a', '')} × {speakers.get('b', '')}")
        self._conversation_meta.setText(
            f"会话 {payload.get('selected_session', 1)} · {len(messages)} 条记录 · 数据源 {dataset.get('name', 'LoCoMo-10')}"
        )
        question = self._question_combo.currentData()
        if isinstance(question, dict) and not self._query.toPlainText().strip():
            self._query.setPlainText(question.get("question", ""))
        self._render_messages()
        self._render_results(payload.get("preview_results") or [], preview=True)
        mem0 = payload.get("mem0") or {}
        if mem0.get("live"):
            summary = f"Mem0 在线 · {mem0.get('mode', 'local-oss')} · {mem0.get('collection', 'LoCoMo 索引')}"
            self._set_status(summary)
        else:
            self._set_status("Mem0 离线 · 右侧仅为已有评测快照；运行召回前请启动本地 Mem0 服务", warning=True)
        self._set_pipeline()

    def _render_messages(self) -> None:
        _clear_layout(self._message_layout)
        messages = self._base_messages + self._transient_messages
        for message in messages:
            outgoing = message.get("role") == "user"
            row = QHBoxLayout()
            if outgoing:
                row.addStretch(1)
            bubble = QFrame()
            bubble.setObjectName("MemoryBubbleUser" if outgoing else "MemoryBubbleAssistant")
            bubble.setMaximumWidth(640)
            bubble_lay = QVBoxLayout(bubble)
            bubble_lay.setContentsMargins(12, 9, 12, 9)
            bubble_lay.setSpacing(4)
            meta = QLabel(
                f"{message.get('speaker', '用户' if outgoing else 'Mem0')}  ·  "
                f"{message.get('id') or message.get('date_time', '')}"
            )
            meta.setObjectName("MemoryBubbleMeta")
            text = QLabel(str(message.get("text") or ""))
            text.setObjectName("MemoryBubbleText")
            text.setWordWrap(True)
            text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            bubble_lay.addWidget(meta)
            bubble_lay.addWidget(text)
            row.addWidget(bubble)
            if not outgoing:
                row.addStretch(1)
            wrapper = QWidget()
            wrapper.setObjectName("MemoryMessageRow")
            wrapper.setLayout(row)
            self._message_layout.addWidget(wrapper)
        self._message_layout.addStretch(1)
        self._message_scroll.verticalScrollBar().setValue(
            self._message_scroll.verticalScrollBar().maximum()
        )

    def _render_results(self, results: list[dict[str, Any]], *, preview: bool = False) -> None:
        _clear_layout(self._recall_layout)
        self._result_badge.setText(str(len(results)))
        self._recall_title.setText("召回预览" if preview else "本轮召回")
        self._recall_meta.setText(
            f"{len(results)} 条 · {'历史评测快照' if preview else '按融合相关度排序'}"
        )
        if not results:
            empty = QLabel("当前问题还没有召回结果。\n点击“运行 Mem0 召回”执行实时检索。")
            empty.setObjectName("MemoryEmpty")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setWordWrap(True)
            self._recall_layout.addWidget(empty, 1)
            return
        for number, result in enumerate(results, 1):
            card = QFrame()
            card.setObjectName("MemoryRecallCard")
            lay = QVBoxLayout(card)
            lay.setContentsMargins(14, 11, 14, 11)
            lay.setSpacing(7)
            top = QHBoxLayout()
            rank = QLabel(f"#{number}")
            rank.setObjectName("MemoryRecallRank")
            score = QLabel(f"{float(result.get('score') or 0.0):.3f}")
            score.setObjectName("MemoryRecallScore")
            top.addWidget(rank)
            top.addStretch(1)
            top.addWidget(score)
            lay.addLayout(top)
            content = QLabel(str(result.get("memory") or result.get("data") or ""))
            content.setObjectName("MemoryRecallText")
            content.setWordWrap(True)
            content.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            lay.addWidget(content)
            breakdown = result.get("score_debug") or result.get("score_breakdown") or {}
            metadata = result.get("metadata") or {}
            details = " · ".join(filter(None, (
                str(result.get("memory_type") or "memory"),
                str(result.get("source") or "mem0"),
                str(metadata.get("session") or metadata.get("source") or "LoCoMo"),
            )))
            if breakdown:
                semantic = breakdown.get("semantic", breakdown.get("semantic_score", breakdown.get("vector", 0)))
                keyword = breakdown.get("keyword", breakdown.get("bm25", breakdown.get("bm25_score", 0)))
                recent = breakdown.get("recency", breakdown.get("recent", 0))
                details += (f" · 关键词 {float(keyword or 0):.2f} / "
                            f"语义 {float(semantic or 0):.2f} / 最近 {float(recent or 0):.2f}")
            meta = QLabel(details)
            meta.setObjectName("MemoryRecallMeta")
            meta.setWordWrap(True)
            lay.addWidget(meta)
            self._recall_layout.addWidget(card)
        self._recall_layout.addStretch(1)

    def _run_recall(self) -> None:
        if self._recall_task is not None:
            return
        query = self._query.toPlainText().strip()
        if not query:
            self._set_status("请输入要召回的问题", warning=True)
            return
        question = self._question_combo.currentData()
        question_index = int(question.get("index", 0)) if isinstance(question, dict) else 0
        case_index = int(self._case_combo.currentData() or 0)
        limit = self._limit.value()
        self._run_button.setEnabled(False)
        self._run_button.setText("Mem0 召回中…")
        self._set_pipeline(active="recall")
        self._set_status("正在执行项目 Mem0 的 Memory.search 混合召回…")
        task = _RecallTask(lambda: self.bridge.recall_memory_benchmark(
            case_index=case_index,
            question_index=question_index,
            query=query,
            limit=limit,
            session_index=int(self._session_combo.currentData() or 1),
            across_sessions=self._across_sessions.isChecked(),
        ))
        task.signals.completed.connect(self._apply_recall)
        task.signals.failed.connect(self._recall_failed)
        self._recall_task = task
        QThreadPool.globalInstance().start(task)

    @Slot(object)
    def _apply_recall(self, response: object) -> None:
        data = dict(response or {})
        self._recall_task = None
        self._run_button.setEnabled(True)
        self._run_button.setText("运行 Mem0 召回")
        self._render_results(list(data.get("results") or []))
        scope = "跨会话" if data.get("scope") == "cross_session" else f"会话 {data.get('session_index', 1)}"
        self._recall_meta.setText(f"{data.get('result_count', 0)} 条命中 · {scope} · 按融合相关度排序")
        self._set_pipeline(stages=list(data.get("stages") or []))
        query = str(data.get("query") or self._query.toPlainText())
        answer = str(data.get("answer") or "")
        ground_truth = str(data.get("ground_truth_answer") or "")
        evidence = "、".join(str(item) for item in (data.get("evidence") or []))
        if ground_truth:
            answer += f"\n\nLoCoMo 标准答案：{ground_truth}"
        if evidence:
            answer += f"\n证据：{evidence}"
        self._transient_messages.extend((
            {"role": "user", "speaker": "查询", "id": "live", "text": query},
            {"role": "assistant", "speaker": "Mem0", "id": data.get("engine", "Mem0 OSS"), "text": answer},
        ))
        self._render_messages()
        mode = "实时 Mem0" if data.get("live") else "Mem0 评测快照"
        status = f"{mode} · {data.get('result_count', 0)} 条 · {data.get('latency_ms', 0)} ms"
        if data.get("warning"):
            status += f" · {data['warning']}"
        self._set_status(status, warning=not bool(data.get("live")))

    @Slot(str)
    def _recall_failed(self, message: str) -> None:
        self._recall_task = None
        self._run_button.setEnabled(True)
        self._run_button.setText("运行 Mem0 召回")
        self._set_pipeline()
        self._set_status(f"召回失败：{message}", error=True)

    def _set_status(self, text: str, *, warning: bool = False, error: bool = False) -> None:
        name = "MemoryStatusError" if error else "MemoryStatusWarning" if warning else "MemoryStatus"
        self._set_object_name(self._status, name)
        self._status.setText(text)
