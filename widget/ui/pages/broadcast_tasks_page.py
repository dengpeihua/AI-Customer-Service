from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, QDateTime
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QDialog,
                               QListWidget, QListWidgetItem, QPushButton, QMessageBox,
                               QTextEdit, QTableWidget, QTableWidgetItem, QHeaderView)

from widget.broadcast.contacts import ContactSource
from widget.broadcast.engine import (is_cancellable, is_deletable, is_resumable,
                                     remaining_recipients)
from widget.broadcast.models import BroadcastTask, delete_task, load_tasks, upsert_task


def _fmt_ts(ts) -> str:
    if not ts:
        return "—"
    return QDateTime.fromSecsSinceEpoch(int(ts)).toString("MM-dd HH:mm:ss")

_STATUS_LABEL = {"draft": "草稿", "scheduled": "已排期", "running": "发送中",
                  "paused": "已暂停", "done": "已完成", "cancelled": "已取消"}


class BroadcastTasksPage(QWidget):
    """群发任务清单：列出全部任务及执行状态，可取消定时/继续/重试/删除/看详情。

    「继续/重试」不在本页自建发送 UI，而是回调 on_run_task(task, mode) 委托给群发页
    （复用其发送线程/进度/暂停/失败重试机器），由外层切到群发页展示进度。
    """

    def __init__(self, tasks_path: str,
                 on_run_task: Callable[[BroadcastTask, str], None] | None = None,
                 source: ContactSource | None = None):
        super().__init__()
        self.tasks_path = tasks_path
        self.on_run_task = on_run_task
        self.source = source          # 用于把收件人 wxid 显示成备注/昵称（可选）
        self._tasks: list[BroadcastTask] = []

        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        t = QLabel("群发任务"); t.setObjectName("Title"); top.addWidget(t)
        top.addStretch(1)
        reload_btn = QPushButton("刷新"); reload_btn.setObjectName("Ghost")
        reload_btn.clicked.connect(self.refresh)
        top.addWidget(reload_btn)
        lay.addLayout(top)

        self._list = QListWidget()
        self._list.currentRowChanged.connect(self._sync_buttons)
        lay.addWidget(self._list, 1)

        btn_row = QHBoxLayout()
        self._detail_btn = self._action_btn("详情", self._on_detail, btn_row)
        self._resume_btn = self._action_btn("继续", self._on_resume, btn_row)
        self._retry_btn = self._action_btn("重试", self._on_retry, btn_row)
        self._cancel_btn = self._action_btn("取消定时", self._on_cancel, btn_row)
        self._delete_btn = self._action_btn("删除记录", self._on_delete, btn_row)
        lay.addLayout(btn_row)

        self.refresh()

    def _action_btn(self, text: str, slot, row: QHBoxLayout) -> QPushButton:
        b = QPushButton(text); b.setObjectName("Ghost"); b.setEnabled(False)
        b.clicked.connect(slot)
        row.addWidget(b)
        return b

    # ---------- 数据 ----------
    def refresh(self) -> None:
        cur = self._list.currentRow()
        self._tasks = sorted(load_tasks(self.tasks_path),
                             key=lambda t: t.created_at, reverse=True)   # 最新在上
        self._list.blockSignals(True)
        self._list.clear()
        for task in self._tasks:
            self._list.addItem(QListWidgetItem(self._row_text(task)))
        self._list.blockSignals(False)
        if self._tasks:
            self._list.setCurrentRow(min(max(cur, 0), len(self._tasks) - 1))
        self._sync_buttons()

    def _row_text(self, task: BroadcastTask) -> str:
        status = _STATUS_LABEL.get(task.status, task.status)
        preview = (task.template or "").replace("\n", " ")[:16]
        parts = [f"[{status}]", preview,
                 f"· {len(task.recipients)}人 发{len(task.sent)}/失败{len(task.failed)}"]
        rem = len(remaining_recipients(task))
        if task.status == "paused" and rem:
            parts.append(f"· 剩{rem}未发")
        if task.scheduled_at is not None:
            when = QDateTime.fromSecsSinceEpoch(int(task.scheduled_at)).toString("MM-dd HH:mm")
            parts.append(f"· 定时 {when}")
        return "  ".join(parts)

    def _current(self) -> BroadcastTask | None:
        i = self._list.currentRow()
        return self._tasks[i] if 0 <= i < len(self._tasks) else None

    def _sync_buttons(self, *_a) -> None:
        task = self._current()
        self._detail_btn.setEnabled(task is not None)
        self._resume_btn.setEnabled(bool(task) and is_resumable(task))
        self._retry_btn.setEnabled(bool(task) and bool(task.failed))
        self._cancel_btn.setEnabled(bool(task) and is_cancellable(task))
        self._delete_btn.setEnabled(bool(task) and is_deletable(task))

    # ---------- 操作 ----------
    def _name_map(self) -> dict:
        if self.source is None:
            return {}
        try:
            return {f.wxid: (f.remark or f.nick or f.wxid) for f in self.source.list_friends()}
        except Exception:
            return {}

    def _on_detail(self) -> None:
        task = self._current()
        if not task:
            return
        _TaskDetailDialog(task, self._name_map(), self).exec()

    def _on_resume(self) -> None:
        task = self._current()
        if task and is_resumable(task) and self.on_run_task:
            self.on_run_task(task, "resume")

    def _on_retry(self) -> None:
        task = self._current()
        if task and task.failed and self.on_run_task:
            self.on_run_task(task, "retry")

    def _on_cancel(self) -> None:
        task = self._current()
        if not task or not is_cancellable(task):
            return
        if QMessageBox.question(self, "取消定时任务", "取消这个尚未发送的定时任务？") \
                != QMessageBox.StandardButton.Yes:
            return
        task.status = "cancelled"
        upsert_task(self.tasks_path, task)
        self.refresh()

    def _on_delete(self) -> None:
        task = self._current()
        if not task or not is_deletable(task):
            return
        if QMessageBox.question(self, "删除记录", "从记录里删除这条已结束的任务？") \
                != QMessageBox.StandardButton.Yes:
            return
        delete_task(self.tasks_path, task.id)
        self.refresh()


class _TaskDetailDialog(QDialog):
    """任务详情：概况 + 文案全文 + 逐收件人（备注名 / 状态 / 时间）表格。"""

    def __init__(self, task: BroadcastTask, name_map: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("群发任务详情")
        self.resize(560, 560)
        lay = QVBoxLayout(self)

        status = _STATUS_LABEL.get(task.status, task.status)
        rem = len(remaining_recipients(task))
        summary = (f"状态：{status}　收件人 {len(task.recipients)} 人　"
                   f"成功 {len(task.sent)}　失败 {len(task.failed)}　未发 {rem}")
        lay.addWidget(QLabel(summary))
        meta = f"创建：{_fmt_ts(task.created_at)}"
        if task.scheduled_at is not None:
            meta += f"　定时：{_fmt_ts(task.scheduled_at)}"
        times = [t for t in task.processed_at.values() if t]
        if times:
            meta += f"　发送时段：{_fmt_ts(min(times))} ~ {_fmt_ts(max(times))}"
        m = QLabel(meta); m.setObjectName("Muted"); lay.addWidget(m)

        lay.addWidget(QLabel("文案"))
        body = QTextEdit(); body.setReadOnly(True); body.setPlainText(task.template or "（空）")
        body.setMaximumHeight(110); lay.addWidget(body)

        lay.addWidget(QLabel("收件人明细"))
        failed_reason = {w: r for w, r in task.failed}
        sent_set = set(task.sent)
        table = QTableWidget(len(task.recipients), 3)
        table.setHorizontalHeaderLabels(["收件人", "状态", "时间"])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for row, wxid in enumerate(task.recipients):
            if wxid in sent_set:
                st = "已发送"
            elif wxid in failed_reason:
                st = f"失败：{failed_reason[wxid]}"
            else:
                st = "未发送"
            name = name_map.get(wxid, wxid)
            table.setItem(row, 0, QTableWidgetItem(name if name == wxid else f"{name}（{wxid}）"))
            table.setItem(row, 1, QTableWidgetItem(st))
            table.setItem(row, 2, QTableWidgetItem(_fmt_ts(task.processed_at.get(wxid))))
        lay.addWidget(table, 1)

        close = QPushButton("关闭"); close.setObjectName("Ghost"); close.clicked.connect(self.accept)
        lay.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)
