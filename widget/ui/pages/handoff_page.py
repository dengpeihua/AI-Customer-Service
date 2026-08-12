from __future__ import annotations
from PySide6.QtCore import QSize
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QTextEdit, QPushButton)
from widget.handoff import HandoffController

# legacy 兜底（单实例/未接注册表时的旧文案）；per-instance 场景由构造时注入的 labels 覆盖。
_CH = {"wechat_personal": "个人微信", "wecom_hook": "企微"}


class HandoffPage(QWidget):
    def __init__(self, controller: HandoffController, avatar_provider=None, on_open_chat=None,
                 adapter=None, labels=None):
        super().__init__()
        self.ctrl = controller
        self.avatars = avatar_provider
        self.on_open_chat = on_open_chat
        self.adapter = adapter          # 用于把待人工客户的 wxid 显示成微信名
        self._names: dict[str, str] = {}
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
        send = QPushButton("回复并结束"); send.setObjectName("Primary"); send.clicked.connect(self._on_send)
        dismiss = QPushButton("忽略"); dismiss.setObjectName("Ghost"); dismiss.clicked.connect(self._on_dismiss)
        row.addWidget(send); row.addWidget(dismiss); right.addLayout(row)
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
        p = self._current()
        if not p:
            return
        text = self._edit.toPlainText().strip()
        if not text:
            self._status.setText("请先输入回复内容"); return
        ok = self.ctrl.reply_pending(p["id"], text)
        self._status.setText("已回复并结束这一条待办" if ok else "发送失败，请重试")
        if ok:
            self._edit.clear()
        self.refresh()

    def _on_dismiss(self) -> None:
        p = self._current()
        if not p:
            return
        self.ctrl.dismiss_pending(p["id"])
        self._status.setText("已标记这一条处理完"); self._edit.clear(); self.refresh()
