from __future__ import annotations

import threading

from PySide6.QtCore import Qt, QDateTime, QSize, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QTextEdit, QLineEdit,
                               QPushButton, QRadioButton, QButtonGroup, QDateTimeEdit,
                               QProgressBar, QMessageBox, QInputDialog)

from widget.bridge import Bridge, BridgeError
from widget.broadcast.contacts import ContactSource, Friend
from widget.broadcast.engine import (is_resumable, remaining_recipients,
                                      render_template, run_broadcast)
from widget.broadcast.models import BroadcastTask, load_tasks, new_task, upsert_task
from widget.config import BroadcastConfig
from widget.sender import RateLimiter

_STATUS_LABEL = {"draft": "草稿", "scheduled": "已排期", "running": "发送中",
                  "paused": "已暂停", "done": "已完成", "cancelled": "已取消"}


def broadcast_title(account_label: str) -> str:
    """群发页标题。M4：副表盘钉「主实例」，标题必须写明是哪个号在发，别装作全局。"""
    return f"群发 · 当前账号：{account_label}" if account_label else "群发"


class BroadcastPage(QWidget):
    """群发页：文案（含 AI 帮写）+ 名单勾选 + 立即/定时 + 后台发送 + 进度/失败重试。

    发送在后台线程跑 run_broadcast（内含 sleep，会阻塞），进度经 Qt 信号
    （跨线程 emit 自动排队到本对象所在的 GUI 线程）回调更新界面，绝不在
    子线程里直接碰 widget —— 与 widget/app.py 里 _Signals 的套路一致。
    """

    _progress_sig = Signal(int, int, str)
    _finished_sig = Signal(object)
    _draft_ready_sig = Signal(str)
    _draft_error_sig = Signal(str)
    _error_sig = Signal(str)
    _friends_ready_sig = Signal(object)
    _friends_error_sig = Signal(str)

    def __init__(self, bridge: Bridge, source: ContactSource, bcfg: BroadcastConfig,
                 adapter, tasks_path: str, limiter: RateLimiter, signals=None,
                 avatar_provider=None, on_open_chat=None, account_label: str = ""):
        super().__init__()
        self.bridge = bridge
        self.account_label = account_label
        self.source = source
        self.bcfg = bcfg
        self.adapter = adapter
        self.tasks_path = tasks_path
        self.limiter = limiter
        self.signals = signals   # 复用 app 的刷新信号（可选，用于任务完成后提醒工作台其它页）
        self.avatars = avatar_provider
        self.on_open_chat = on_open_chat

        self._friends: list[Friend] = []
        self._tasks: list[BroadcastTask] = load_tasks(tasks_path)
        self._current_task: BroadcastTask | None = None
        self._stop_event: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._friends_refreshing = False

        self._progress_sig.connect(self._on_progress)
        self._finished_sig.connect(self._on_finished)
        self._draft_ready_sig.connect(self._on_draft_ready)
        self._draft_error_sig.connect(self._on_draft_error)
        self._error_sig.connect(self._on_send_error)
        self._friends_ready_sig.connect(self._on_friends_ready)
        self._friends_error_sig.connect(self._on_friends_error)

        self._build_ui()
        self.refresh_friends()
        self._load_resumable()   # 挂件重启后，让上次没发完的群发可继续

    # ---------- UI 构建 ----------
    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        t = QLabel(broadcast_title(self.account_label)); t.setObjectName("Title"); lay.addWidget(t)
        body = QHBoxLayout(); lay.addLayout(body, 1)

        # 左：文案 + 名单
        left = QVBoxLayout(); body.addLayout(left, 1)

        left.addWidget(QLabel("文案"))
        edit_row = QHBoxLayout()
        self._template_edit = QTextEdit()
        self._template_edit.setPlaceholderText("输入群发文案，例如：{昵称}你好，本店周年庆全场8折～")
        edit_row.addWidget(self._template_edit, 1)
        ai_col = QVBoxLayout()
        self._ai_btn = QPushButton("AI 帮写"); self._ai_btn.setObjectName("Ghost")
        self._ai_btn.clicked.connect(self._on_ai_draft)
        ai_col.addWidget(self._ai_btn); ai_col.addStretch(1)
        edit_row.addLayout(ai_col)
        left.addLayout(edit_row, 2)
        hint = QLabel("可用变量：{昵称} {备注}"); hint.setObjectName("Muted")
        left.addWidget(hint)

        left.addWidget(QLabel("收件人（名单来自好友库；非好友已置灰不可选）"))
        filter_row = QHBoxLayout()
        self._search_edit = QLineEdit(); self._search_edit.setPlaceholderText("按备注/昵称搜索…")
        self._search_edit.textChanged.connect(self._apply_filter)
        filter_row.addWidget(self._search_edit, 1)
        sel_all = QPushButton("全选"); sel_all.setObjectName("Ghost"); sel_all.clicked.connect(self._select_all)
        sel_none = QPushButton("清空"); sel_none.setObjectName("Ghost"); sel_none.clicked.connect(self._select_none)
        filter_row.addWidget(sel_all); filter_row.addWidget(sel_none)
        left.addLayout(filter_row)
        self._friend_list = QListWidget()
        self._friend_list.setIconSize(QSize(32, 32))
        self._friend_list.itemDoubleClicked.connect(self._on_friend_double_click)
        if self.avatars is not None:
            self.avatars.avatar_ready.connect(self._on_avatar_ready)
        left.addWidget(self._friend_list, 3)
        self._reload_btn = QPushButton("刷新好友列表"); self._reload_btn.setObjectName("Ghost")
        self._reload_btn.clicked.connect(self.refresh_friends)
        left.addWidget(self._reload_btn)

        # 右：定时 + 发送 + 进度 + 失败
        right = QVBoxLayout(); body.addLayout(right, 1)

        right.addWidget(QLabel("发送时机"))
        self._radio_now = QRadioButton("立即发送")
        self._radio_later = QRadioButton("定时发送")
        self._radio_now.setChecked(True)
        sched_group = QButtonGroup(self)
        sched_group.addButton(self._radio_now); sched_group.addButton(self._radio_later)
        self._sched_group = sched_group   # 防止 QButtonGroup 被垃圾回收
        right.addWidget(self._radio_now); right.addWidget(self._radio_later)
        self._datetime_edit = QDateTimeEdit(QDateTime.currentDateTime().addSecs(3600))
        self._datetime_edit.setCalendarPopup(True)
        self._datetime_edit.setEnabled(False)
        right.addWidget(self._datetime_edit)
        self._warn_label = QLabel("定时发送需保持本机开机且挂件运行")
        self._warn_label.setStyleSheet("color: #D33333; font-weight: 600;")
        self._warn_label.setVisible(False)
        right.addWidget(self._warn_label)
        self._radio_later.toggled.connect(self._on_schedule_toggled)

        btn_row = QHBoxLayout()
        self._send_btn = QPushButton("发送"); self._send_btn.setObjectName("Primary")
        self._send_btn.clicked.connect(self._on_send_clicked)
        self._stop_btn = QPushButton("停止"); self._stop_btn.setObjectName("Ghost")
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._on_stop_clicked)
        btn_row.addWidget(self._send_btn); btn_row.addWidget(self._stop_btn)
        right.addLayout(btn_row)

        self._progress_bar = QProgressBar(); self._progress_bar.setValue(0)
        right.addWidget(self._progress_bar)
        self._status_label = QLabel(""); self._status_label.setObjectName("Muted")
        right.addWidget(self._status_label)

        right.addWidget(QLabel("失败项"))
        self._failed_list = QListWidget()
        right.addWidget(self._failed_list, 1)
        result_btn_row = QHBoxLayout()
        self._resume_btn = QPushButton("继续发送"); self._resume_btn.setObjectName("Ghost")
        self._resume_btn.setEnabled(False)
        self._resume_btn.clicked.connect(self._on_resume_clicked)
        result_btn_row.addWidget(self._resume_btn)
        self._retry_btn = QPushButton("重试失败项"); self._retry_btn.setObjectName("Ghost")
        self._retry_btn.setEnabled(False)
        self._retry_btn.clicked.connect(self._on_retry_clicked)
        result_btn_row.addWidget(self._retry_btn)
        right.addLayout(result_btn_row)

    # ---------- 名单 ----------
    def refresh_friends(self) -> None:
        if self._friends_refreshing:
            return
        self._friends_refreshing = True
        self._reload_btn.setEnabled(False)
        self._reload_btn.setText("刷新中...")

        def worker() -> None:
            try:
                friends = self.source.list_friends()
            except Exception as e:
                self._friends_error_sig.emit(str(e))
                return
            self._friends_ready_sig.emit(friends)

        threading.Thread(target=worker, daemon=True).start()

    def _on_friends_ready(self, friends) -> None:
        self._friends_refreshing = False
        self._reload_btn.setEnabled(True)
        self._reload_btn.setText("刷新好友列表")
        self._friends = list(friends)
        self._render_friends()

    def _on_friends_error(self, message: str) -> None:
        self._friends_refreshing = False
        self._reload_btn.setEnabled(True)
        self._reload_btn.setText("刷新好友列表")
        self._friends = []
        self._render_friends()
        self._status_label.setText(f"读取好友失败: {message[:120]}")

    def _render_friends(self) -> None:
        self._friend_list.clear()
        for f in self._friends:
            label = f.remark or f.nick or f.wxid
            item = QListWidgetItem(f"{label} ({f.wxid})")
            item.setData(Qt.ItemDataRole.UserRole, f)
            if self.avatars is not None:
                item.setIcon(QIcon(self.avatars.get_pixmap(f.wxid, label)))
            if f.is_friend:
                item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled
                              | Qt.ItemFlag.ItemIsSelectable)
                item.setCheckState(Qt.CheckState.Unchecked)
            else:
                item.setFlags(Qt.ItemFlag.NoItemFlags)   # 非好友：置灰不可选
                item.setForeground(Qt.GlobalColor.gray)
            self._friend_list.addItem(item)
        self._apply_filter(self._search_edit.text())

    def _on_friend_double_click(self, item) -> None:
        f = item.data(Qt.ItemDataRole.UserRole)
        if f and self.on_open_chat:
            self.on_open_chat(f.wxid)      # 跳到和该好友的会话（无会话则不跳）

    def _on_avatar_ready(self, wxid: str) -> None:
        for i in range(self._friend_list.count()):
            it = self._friend_list.item(i)
            f = it.data(Qt.ItemDataRole.UserRole)
            if f and f.wxid == wxid:
                it.setIcon(QIcon(self.avatars.get_pixmap(wxid, f.remark or f.nick or wxid)))

    def _apply_filter(self, text: str) -> None:
        text = (text or "").strip().lower()
        for i in range(self._friend_list.count()):
            item = self._friend_list.item(i)
            f: Friend = item.data(Qt.ItemDataRole.UserRole)
            hay = f"{f.remark} {f.nick} {f.wxid}".lower()
            item.setHidden(bool(text) and text not in hay)

    def _select_all(self) -> None:
        for i in range(self._friend_list.count()):
            item = self._friend_list.item(i)
            if item.isHidden():
                continue
            if item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                item.setCheckState(Qt.CheckState.Checked)

    def _select_none(self) -> None:
        for i in range(self._friend_list.count()):
            item = self._friend_list.item(i)
            if item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                item.setCheckState(Qt.CheckState.Unchecked)

    def _selected_wxids(self) -> list[str]:
        out = []
        for i in range(self._friend_list.count()):
            item = self._friend_list.item(i)
            if (item.flags() & Qt.ItemFlag.ItemIsUserCheckable) and \
               item.checkState() == Qt.CheckState.Checked:
                f: Friend = item.data(Qt.ItemDataRole.UserRole)
                out.append(f.wxid)
        return out

    # ---------- AI 帮写 ----------
    def _on_ai_draft(self) -> None:
        brief, ok = QInputDialog.getText(self, "AI 帮写文案", "简单描述你要发送的内容：")
        if not ok or not brief.strip():
            return
        self._ai_btn.setEnabled(False)
        brief = brief.strip()

        def worker() -> None:
            try:
                draft = self.bridge.draft_broadcast(brief)
                self._draft_ready_sig.emit(draft)
            except BridgeError as e:
                self._draft_error_sig.emit(str(e))

        threading.Thread(target=worker, daemon=True).start()

    def _on_draft_ready(self, draft: str) -> None:
        self._ai_btn.setEnabled(True)
        self._template_edit.setPlainText(draft)

    def _on_draft_error(self, msg: str) -> None:
        self._ai_btn.setEnabled(True)
        QMessageBox.warning(self, "AI 帮写失败", f"生成失败：{msg}")

    # ---------- 定时 ----------
    def _on_schedule_toggled(self, checked: bool) -> None:
        self._datetime_edit.setEnabled(checked)
        self._warn_label.setVisible(checked)

    # ---------- 发送 ----------
    def _on_send_clicked(self) -> None:
        template = self._template_edit.toPlainText().strip()
        if not template:
            QMessageBox.warning(self, "提示", "请先填写群发文案"); return
        recipients = self._selected_wxids()
        if not recipients:
            QMessageBox.warning(self, "提示", "请至少勾选一个收件人"); return

        friends_by_id = {f.wxid: f for f in self._friends}
        sample = friends_by_id.get(recipients[0])
        sample_text = render_template(template, sample) if sample else template

        scheduled_at = None
        preview = f"将向 {len(recipients)} 人发送：\n\n示例：\n{sample_text}"
        if self._radio_later.isChecked():
            scheduled_at = self._datetime_edit.dateTime().toSecsSinceEpoch()
            preview += f"\n\n定时于 {self._datetime_edit.dateTime().toString('yyyy-MM-dd HH:mm')} 发送"

        ret = QMessageBox.question(self, "发送确认", preview,
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                    QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return

        task = new_task(template, recipients, scheduled_at=scheduled_at)
        self._tasks.append(task)   # 仅用于本页展示；落盘走 upsert，不整表覆盖
        upsert_task(self.tasks_path, task)

        if scheduled_at is not None:
            QMessageBox.information(self, "已安排",
                "已创建定时任务，将在到时由挂件自动发送（需保持本机开机、挂件运行）。")
            return

        self._start_send(task)

    def _start_send(self, task: BroadcastTask) -> None:
        self._current_task = task
        self._stop_event = threading.Event()
        self._progress_bar.setMaximum(max(len(task.recipients), 1))
        self._progress_bar.setValue(len(task.sent))
        self._send_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._retry_btn.setEnabled(False)
        self._resume_btn.setEnabled(False)
        self._failed_list.clear()
        self._status_label.setText("发送中…")
        self._thread = threading.Thread(target=self._run_worker, args=(task,), daemon=True)
        self._thread.start()

    def _run_worker(self, task: BroadcastTask) -> None:
        stop_event = self._stop_event
        try:
            final = run_broadcast(
                task, self.source, self.adapter, self.bcfg,
                limiter=self.limiter,
                should_stop=stop_event.is_set,
                on_progress=lambda done, total, result: self._progress_sig.emit(done, total, result),
                save_fn=lambda t: upsert_task(self.tasks_path, t),
            )
        except Exception as e:
            self._error_sig.emit(str(e))
            return
        self._finished_sig.emit(final)

    def _on_progress(self, done: int, total: int, result: str) -> None:
        self._progress_bar.setMaximum(max(total, 1))
        self._progress_bar.setValue(done)
        self._status_label.setText(f"{done}/{total} · {result}")

    def _on_finished(self, task: BroadcastTask) -> None:
        self._current_task = task
        self._send_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._reflect_result(task)
        if self.signals is not None:
            self.signals.refresh.emit()

    def _reflect_result(self, task: BroadcastTask) -> None:
        """把一个任务的结果状态显示到进度/失败/按钮上（发送结束或页面载入未完成任务时复用）。"""
        label = _STATUS_LABEL.get(task.status, task.status)
        remaining = remaining_recipients(task)
        suffix = f" / 未发 {len(remaining)}" if remaining else ""
        self._status_label.setText(
            f"{label}：成功 {len(task.sent)} / 失败 {len(task.failed)}{suffix}")
        self._progress_bar.setMaximum(max(len(task.recipients), 1))
        self._progress_bar.setValue(len(task.sent))
        self._failed_list.clear()
        for wxid, reason in task.failed:
            self._failed_list.addItem(f"{wxid}：{reason}")
        self._retry_btn.setEnabled(bool(task.failed))
        self._resume_btn.setEnabled(is_resumable(task))   # 暂停且还有没发的 → 可继续

    def _on_send_error(self, msg: str) -> None:
        # 后台发送线程抛异常：不再静默吞掉，明确提示用户当前批次可能未完整发送。
        self._send_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._status_label.setText("发送出错")
        QMessageBox.warning(self, "群发出错", f"发送过程中出现错误：{msg}")

    def _on_stop_clicked(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
            self._status_label.setText("正在停止…")

    def _on_retry_clicked(self) -> None:
        if not self._current_task or not self._current_task.failed:
            return
        retry_ids = [f[0] for f in self._current_task.failed]
        task = new_task(self._current_task.template, retry_ids)
        self._tasks.append(task)   # 仅用于本页展示；落盘走 upsert，不整表覆盖
        upsert_task(self.tasks_path, task)
        self._start_send(task)

    def _on_resume_clicked(self) -> None:
        # 继续发送：对同一个暂停任务再跑一次，引擎会跳过已发的、把剩下的发完（含上次失败的）
        task = self._current_task
        if task is None or not is_resumable(task):
            return
        remaining = len(remaining_recipients(task))
        ret = QMessageBox.question(
            self, "继续发送", f"继续把剩余 {remaining} 位未发送的收件人发完？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        self._start_send(task)

    def _load_resumable(self) -> None:
        """页面载入时：若磁盘上有未完成（暂停且尚有未发）任务，取最近一个反映出来，让商家可继续。
        （挂件重启后，上次没发完的群发不至于石沉大海。）"""
        candidates = [t for t in self._tasks if is_resumable(t)]
        if not candidates:
            return
        task = max(candidates, key=lambda t: t.created_at)
        self._current_task = task
        self._reflect_result(task)

    # ---------- 供「群发任务」页回调复用（继续/重试同一发送机器） ----------
    def resume_task(self, task: BroadcastTask) -> None:
        """继续发送一个暂停任务：对同一 task 再跑，引擎跳过已发、发完剩余。"""
        self._current_task = task
        self._start_send(task)

    def retry_task(self, task: BroadcastTask) -> None:
        """重试一个任务的失败项：用失败 wxid 建新任务发送。"""
        retry_ids = [f[0] for f in task.failed]
        if not retry_ids:
            return
        new = new_task(task.template, retry_ids)
        upsert_task(self.tasks_path, new)
        self._start_send(new)
