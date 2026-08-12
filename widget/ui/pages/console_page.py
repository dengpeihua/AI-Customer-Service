"""统一会话控制台（三栏，照 WeiClaw 客服台 + 补渠道维度）。

左：双渠道合并会话列表（渠道徽章 + 待人工置顶）｜中：消息气泡(我方/AI/对方) + 回复框
｜右：客户详情（**AI 托管开关 = 转人工/恢复AI** + 客户标签）。

薄视图：所有数据/动作走注入的 ConversationConsole 模型（已单测）；本文件只管渲染与交互，
GUI 冒烟只需「能构造 + 选中 + 切开关 + 回复不抛异常」。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QPushButton, QScrollArea, QTextEdit,
                               QVBoxLayout, QWidget)
from widget.ui.message_content import message_content_widget

_BADGE_OBJ = {"wechat_personal": "BadgeWechat", "wecom_hook": "BadgeWecom",
              "wechat": "BadgeWechat", "wecom": "BadgeWecom"}


def _badge_obj(channel: str) -> str:
    """徽章颜色按**平台**决定，而非完整 channel_key——per-instance 键形如
    `wecom#a`/`wechat#b`（M0 记法）没有直接命中项，取 `#` 前的平台再查一次；
    legacy 键（`wechat_personal`/`wecom_hook`）直接命中，行为不变。"""
    return _BADGE_OBJ.get(channel) or _BADGE_OBJ.get(channel.split("#")[0], "BadgeWecom")


def _bubble_row(message: dict, is_self: bool, provenance: str = "") -> QWidget:
    row = QWidget(); lay = QHBoxLayout(row); lay.setContentsMargins(2, 2, 2, 2)
    bubble = message_content_widget({**message, "is_self": is_self})
    if is_self:
        lay.addStretch(1)
        if provenance == "ai":                       # 我方 AI 气泡：加小“AI”橙徽章
            tag = QLabel("AI"); tag.setObjectName("AiTag"); tag.setAlignment(Qt.AlignTop)
            lay.addWidget(tag)
        lay.addWidget(bubble)
    else:
        lay.addWidget(bubble); lay.addStretch(1)
    return row


class _ConvRow(QWidget):
    """会话列表条目：名字 + 渠道徽章 + 最后一条 + 待人工红点。"""
    def __init__(self, conv: dict):
        super().__init__()
        lay = QVBoxLayout(self); lay.setContentsMargins(6, 4, 6, 4); lay.setSpacing(2)
        top = QHBoxLayout(); top.setSpacing(6)
        name = QLabel(conv.get("name") or conv.get("contact", "")); name.setObjectName("ConvName")
        top.addWidget(name)
        badge = QLabel(conv.get("channel_label", ""))
        badge.setObjectName(_badge_obj(conv.get("channel", "")))
        top.addWidget(badge)
        if conv.get("pending"):
            dot = QLabel("● 待人工"); dot.setObjectName("PendingDot"); top.addWidget(dot)
        top.addStretch(1)
        lay.addLayout(top)
        last = QLabel((conv.get("last") or "")[:30]); last.setObjectName("ConvLast")
        lay.addWidget(last)


class ConsolePage(QWidget):
    def __init__(self, console_model, avatar_provider=None):
        super().__init__()
        self.model = console_model
        self.avatars = avatar_provider
        self._current: dict | None = None
        self.setObjectName("ConsolePanel")
        root = QHBoxLayout(self); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(10)

        # 左：会话列表
        left = QVBoxLayout()
        t = QLabel("会话"); t.setObjectName("Title"); left.addWidget(t)
        self._list = QListWidget(); self._list.setFixedWidth(240)
        self._list.currentRowChanged.connect(self._on_select)
        left.addWidget(self._list, 1)
        root.addLayout(left)

        # 中：消息 + 回复
        mid = QVBoxLayout()
        self._title = QLabel("（选择左侧一个会话）"); self._title.setObjectName("Title")
        mid.addWidget(self._title)
        self._ai_off_tip = QLabel("AI 托管已关闭，该会话由人工处理"); self._ai_off_tip.setObjectName("AiOffTip")
        self._ai_off_tip.setVisible(False); mid.addWidget(self._ai_off_tip)
        self._scroll = QScrollArea(); self._scroll.setWidgetResizable(True)
        self._msg_host = QWidget(); self._msg_lay = QVBoxLayout(self._msg_host)
        self._msg_lay.addStretch(1); self._scroll.setWidget(self._msg_host)
        mid.addWidget(self._scroll, 1)
        self._edit = QTextEdit(); self._edit.setPlaceholderText("输入人工回复…"); self._edit.setFixedHeight(76)
        mid.addWidget(self._edit)
        row = QHBoxLayout(); row.addStretch(1)
        send = QPushButton("发送"); send.setObjectName("Primary"); send.clicked.connect(self._on_send)
        row.addWidget(send); mid.addLayout(row)
        self._status = QLabel(""); self._status.setObjectName("Muted"); mid.addWidget(self._status)
        root.addLayout(mid, 1)

        # 右：客户详情 + AI 托管开关
        right = QVBoxLayout()
        rt = QLabel("客户详情"); rt.setObjectName("Title"); right.addWidget(rt)
        self._who = QLabel("—"); self._who.setWordWrap(True); right.addWidget(self._who)
        self._ai_switch = QCheckBox("AI 托管（关=转人工，开=恢复AI）")
        self._ai_switch.toggled.connect(self._on_toggle_ai); right.addWidget(self._ai_switch)
        right.addWidget(QLabel("客户标签："))
        self._tags = QLabel("—"); self._tags.setObjectName("Muted"); self._tags.setWordWrap(True)
        right.addWidget(self._tags)
        right.addStretch(1)
        self._summarize_btn = QPushButton("总结这段对话 → 反哺知识库")
        self._summarize_btn.setObjectName("Ghost"); self._summarize_btn.clicked.connect(self._on_summarize)
        right.addWidget(self._summarize_btn)
        # 企微本地库历史反哺 = **后台自动同步**（客户不用点任何按钮），这里只被动显示状态。
        self._sync_status = QLabel(""); self._sync_status.setObjectName("Muted")
        self._sync_status.setWordWrap(True); self._sync_status.setVisible(False)
        right.addWidget(self._sync_status)
        rwrap = QWidget(); rwrap.setFixedWidth(240); rwrap.setLayout(right)
        root.addWidget(rwrap)

        self.refresh()

    # ---- 数据 ----
    def refresh(self) -> None:
        convs = self.model.conversations()
        cur = self._list.currentRow()
        self._list.blockSignals(True)
        self._list.clear()
        for c in convs:
            item = QListWidgetItem()
            item.setData(Qt.UserRole, (c["channel"], c["contact"]))
            row = _ConvRow(c)
            item.setSizeHint(row.sizeHint())
            self._list.addItem(item)
            self._list.setItemWidget(item, row)
        self._list.blockSignals(False)
        if convs:
            self._list.setCurrentRow(min(max(cur, 0), len(convs) - 1))

    def _selected_key(self):
        item = self._list.currentItem()
        return item.data(Qt.UserRole) if item is not None else None

    def _on_select(self, _row: int) -> None:
        key = self._selected_key()
        if not key:
            return
        channel, contact = key
        self._current = {"channel": channel, "contact": contact}
        self._render_sync_status(channel)          # 企微：显示历史自动同步状态（不用点按钮）
        label = self.model.channel_label(channel)
        self._title.setText(f"{contact}  ·  {label}")
        self._who.setText(f"{contact}\n渠道：{label}")
        self._render_messages(channel, contact)
        self._render_detail(channel, contact)

    def _clear_messages(self) -> None:
        while self._msg_lay.count() > 1:                 # 保留末尾 stretch
            it = self._msg_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

    def _render_messages(self, channel: str, contact: str) -> None:
        self._clear_messages()
        for m in self.model.messages(channel, contact):
            self._msg_lay.insertWidget(self._msg_lay.count() - 1,
                                       _bubble_row(m, bool(m.get("is_self")),
                                                    m.get("provenance", "")))

    def _render_detail(self, channel: str, contact: str) -> None:
        d = self.model.detail(channel, contact)
        self._ai_switch.blockSignals(True)              # 只反映状态，不触发写回
        self._ai_switch.setChecked(bool(d.get("ai_enabled", True)))
        self._ai_switch.blockSignals(False)
        self._ai_off_tip.setVisible(not d.get("ai_enabled", True))
        tags = d.get("tags") or []
        names = "、".join(t.get("name", "") for t in tags) if tags else "—"
        self._tags.setText(names)

    # ---- 交互 ----
    def _on_toggle_ai(self, checked: bool) -> None:
        if not self._current:
            return
        try:
            self.model.set_ai_enabled(self._current["channel"], self._current["contact"], checked)
            self._ai_off_tip.setVisible(not checked)
            self._status.setText("已恢复 AI 托管" if checked else "已转人工（AI 托管关闭）")
        except Exception as e:                           # noqa: BLE001
            self._status.setText(f"切换失败：{e}")

    def _on_summarize(self) -> None:
        if not self._current:
            return
        try:
            out = self.model.summarize(self._current["channel"], self._current["contact"])
            self._status.setText(f"已反哺知识库：{out.get('title', '') or '完成'}")
        except Exception as e:                           # noqa: BLE001
            self._status.setText(f"反哺失败：{e}")

    def _render_sync_status(self, channel: str) -> None:
        """企微会话：被动显示本地库历史【自动】反哺状态（后台线程在跑，客户无需点击）。"""
        st = self.model.history_sync_status(channel)
        if st is None:                                   # 非企微 / 未接自动同步器 → 隐藏
            self._sync_status.setVisible(False)
            return
        running = "自动同步中" if st.get("running") else "未运行"
        self._sync_status.setText(f"企微历史反哺：{running} · 已反哺 {st.get('synced', 0)} 条")
        self._sync_status.setVisible(True)

    def _on_send(self) -> None:
        if not self._current:
            return
        text = self._edit.toPlainText().strip()
        if not text:
            self._status.setText("请先输入回复内容"); return
        ok = self.model.reply(self._current["channel"], self._current["contact"], text)
        if ok:
            self._edit.clear()
            self._status.setText("已发送")
            self._render_messages(self._current["channel"], self._current["contact"])
            self.refresh()
        else:
            self._status.setText("发送失败（渠道未连接或桥不可达），请重试")
