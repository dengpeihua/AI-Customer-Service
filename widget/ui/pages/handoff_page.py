from __future__ import annotations
from concurrent.futures import Future, ThreadPoolExecutor

from PySide6.QtCore import QSize, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QTextEdit, QPushButton)
from widget.handoff import HandoffController

# legacy 兜底（单实例/未接注册表时的旧文案）；per-instance 场景由构造时注入的 labels 覆盖。
_CH = {"douyin": "抖音"}


class HandoffPage(QWidget):
    _reply_ready = Signal(object)

    def __init__(self, controller: HandoffController, avatar_provider=None, on_open_chat=None,
                 adapter=None, labels=None):
        super().__init__()
        self.ctrl = controller
        self.avatars = avatar_provider
        self.on_open_chat = on_open_chat
        self.adapter = adapter          # 用于把待人工客户的 wxid 显示成抖音私信名
        self._names: dict[str, str] = {}
        self._reply_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="handoff-reply",
        )
        self._reply_future: Future | None = None
        self._closed = False
        self._reply_ready.connect(self._apply_reply_result)
        # per-instance 标签注册表：{channel_key: display_name}（app 从 instances 注册表建，注入）。
        # 缺省 {} → 回落到 legacy 两平台常量，再回落 ""（未知渠道不显示徽章，行为不变）。
        self.labels: dict = dict(labels or {})
        lay = QVBoxLayout(self)
        t = QLabel("待人工"); t.setObjectName("Title"); lay.addWidget(t)
        body = QHBoxLayout(); lay.addLayout(body, 1)
        self._list = QListWidget(); self._list.currentRowChanged.connect(self._on_select)
        self._list.setIconSize(QSize(32, 32))
        self._list.itemDoubleClicked.connect(self._on_double_click)   # 双击跳到该客户聊天记录
        if self.avatars is not None:
            self.avatars.avatar_ready.connect(self._on_avatar_ready)
        body.addWidget(self._list, 1)
        right = QVBoxLayout(); body.addLayout(right, 1)
        self._customer = QLabel("（选择左侧一条待处理消息）"); self._customer.setWordWrap(True)
        right.addWidget(self._customer)
        self._edit = QTextEdit(); self._edit.setPlaceholderText("输入人工回复…"); right.addWidget(self._edit, 1)
        row = QHBoxLayout()
        self._send_button = QPushButton("回复并结束")
        self._send_button.setObjectName("Primary")
        self._send_button.clicked.connect(self._on_send)
        self._dismiss_button = QPushButton("忽略")
        self._dismiss_button.setObjectName("Ghost")
        self._dismiss_button.clicked.connect(self._on_dismiss)
        row.addWidget(self._send_button)
        row.addWidget(self._dismiss_button)
        right.addLayout(row)
        self._status = QLabel(""); self._status.setObjectName("Muted"); right.addWidget(self._status)
        self.refresh()

    def pending_count(self) -> int:
        return len(self.ctrl.pending())

    def _current(self) -> dict | None:
        i = self._list.currentRow()
        pend = self.ctrl.pending()
        return pend[i] if 0 <= i < len(pend) else None

    def _name(self, wxid: str) -> str:
        if wxid in self._names:
            return self._names[wxid]
        if self.adapter is None:
            return wxid
        try:
            return self.adapter.display_names([wxid]).get(wxid, wxid)
        except Exception:
            return wxid

    def refresh(self) -> None:
        pend = self.ctrl.pending()
        contacts = list(dict.fromkeys(str(p.get("contact") or "") for p in pend if p.get("contact")))
        try:
            self._names = self.adapter.display_names(contacts) if self.adapter is not None else {}
        except Exception:
            self._names = {contact: contact for contact in contacts}
        cur = self._list.currentRow()
        self._list.blockSignals(True)
        self._list.clear()
        for p in pend:
            name = self._name(p["contact"])
            ch = p.get("channel", "")
            badge = self.labels.get(ch, _CH.get(ch, ""))
            prefix = f"[{badge}] " if badge else ""
            kind = "[AI 草稿] " if p.get("kind") == "auto_reply_draft" else ""
            item = QListWidgetItem(f"{prefix}{kind}{name}：{p['text'][:24]}")
            if self.avatars is not None:
                item.setIcon(QIcon(self.avatars.get_pixmap(p["contact"], name)))
            self._list.addItem(item)
        self._list.blockSignals(False)
        if pend:
            self._list.setCurrentRow(min(max(cur, 0), len(pend) - 1))
        else:
            self._customer.setText("（暂无待人工消息）"); self._edit.clear()

    def _on_double_click(self, _item) -> None:
        p = self._current()
        if p and self.on_open_chat:
            self.on_open_chat(p["contact"])

    def _on_avatar_ready(self, wxid: str) -> None:
        for i, p in enumerate(self.ctrl.pending()):
            if p["contact"] == wxid:
                self._list.item(i).setIcon(QIcon(self.avatars.get_pixmap(wxid, self._name(wxid))))

    def _on_select(self, _i: int) -> None:
        p = self._current()
        if p:
            self._customer.setText(f"客户 {self._name(p['contact'])}：{p['text']}")
            if not self._edit.toPlainText().strip():
                self._edit.setPlainText(p.get("draft", ""))

    def _on_send(self) -> None:
        if self._reply_future is not None and not self._reply_future.done():
            return
        p = self._current()
        if not p:
            return
        text = self._edit.toPlainText().strip()
        if not text:
            self._status.setText("请先输入回复内容"); return
        self._set_reply_busy(True)
        self._status.setText("正在发送并核对回执…")
        pending_id = str(p["id"])

        def worker() -> dict:
            try:
                ok = bool(self.ctrl.reply_pending(pending_id, text, notify=False))
                error = str(getattr(self.ctrl, "last_error", "") or "请重试")
                return {"ok": ok, "error": error}
            except Exception as exc:  # noqa: BLE001 - return worker failure to the GUI
                return {"ok": False, "error": str(exc)[:500] or "发送线程异常"}

        future = self._reply_executor.submit(worker)
        self._reply_future = future
        future.add_done_callback(self._emit_reply_result)

    def _emit_reply_result(self, future: Future) -> None:
        if self._closed:
            return
        try:
            result = future.result()
        except Exception as exc:  # noqa: BLE001 - executor failure must be visible
            result = {"ok": False, "error": str(exc)[:500] or "发送线程异常"}
        try:
            self._reply_ready.emit(result)
        except RuntimeError:
            return

    def _apply_reply_result(self, result: dict) -> None:
        self._reply_future = None
        self._set_reply_busy(False)
        if result.get("ok"):
            self._status.setText("已回复并结束这一条待办")
            self._edit.clear()
            notifier = getattr(self.ctrl, "notify_changed", None)
            if callable(notifier):
                notifier()
        else:
            detail = str(result.get("error") or "请重试")
            self._status.setText(f"发送失败：{detail}")
        self.refresh()

    def _set_reply_busy(self, busy: bool) -> None:
        self._send_button.setEnabled(not busy)
        self._dismiss_button.setEnabled(not busy)
        self._list.setEnabled(not busy)
        self._edit.setEnabled(not busy)

    def _on_dismiss(self) -> None:
        p = self._current()
        if not p:
            return
        self.ctrl.dismiss_pending(p["id"])
        self._status.setText("已标记这一条处理完"); self._edit.clear(); self.refresh()

    def closeEvent(self, event) -> None:
        self._closed = True
        self._reply_executor.shutdown(wait=False, cancel_futures=True)
        super().closeEvent(event)
