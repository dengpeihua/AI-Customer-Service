from __future__ import annotations

from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QCheckBox, QPushButton

from widget.state import RuntimeState


class StatusPage(QWidget):
    def __init__(self, state: RuntimeState):
        super().__init__()
        self.state = state
        lay = QVBoxLayout(self)
        t = QLabel("状态设置")
        t.setObjectName("Title")
        lay.addWidget(t)
        self._status = QLabel()
        self._status.setObjectName("Muted")
        self._status.setWordWrap(True)
        lay.addWidget(self._status)
        self._counts = QLabel()
        lay.addWidget(self._counts)
        self._auto = QCheckBox("自动回复总闸（关闭时 AI 只生成草稿，不会发送抖音私信）")
        self._auto.setChecked(state.cfg.auto_send)
        self._auto.clicked.connect(self._toggle_auto)
        lay.addWidget(self._auto)
        self._priv = QCheckBox("处理个人号私信")
        self._priv.setChecked(state.cfg.scope.allow_private)
        self._priv.clicked.connect(lambda: state.set_scope(allow_private=self._priv.isChecked()))
        lay.addWidget(self._priv)
        self._selected_only = QCheckBox("仅接待会话页中已勾选的客户（推荐）")
        self._selected_only.setChecked(state.cfg.scope.private_mode == "selected")
        self._selected_only.clicked.connect(
            lambda: state.set_scope(
                private_mode="selected" if self._selected_only.isChecked() else "all"
            )
        )
        lay.addWidget(self._selected_only)
        self._grp = QCheckBox("处理群组消息（抖音渠道不使用）")
        self._grp.setChecked(state.cfg.scope.allow_group)
        self._grp.clicked.connect(lambda: state.set_scope(allow_group=self._grp.isChecked()))
        lay.addWidget(self._grp)
        nt = QLabel("通知策略")
        nt.setObjectName("Title")
        lay.addWidget(nt)
        self._notify_handoff = QCheckBox("待人工时显示桌面通知")
        self._notify_handoff.setChecked(state.cfg.notifications.handoff)
        self._notify_handoff.clicked.connect(
            lambda: state.set_notifications(handoff=self._notify_handoff.isChecked()))
        lay.addWidget(self._notify_handoff)
        self._notify_auto = QCheckBox("AI 已自动处理时也通知（通常不建议）")
        self._notify_auto.setChecked(state.cfg.notifications.auto_reply)
        self._notify_auto.clicked.connect(
            lambda: state.set_notifications(auto_reply=self._notify_auto.isChecked()))
        lay.addWidget(self._notify_auto)
        self._bring_front = QCheckBox("待人工时自动打开工作台（可能打断当前操作）")
        self._bring_front.setChecked(state.cfg.notifications.bring_to_front)
        self._bring_front.clicked.connect(
            lambda: state.set_notifications(bring_to_front=self._bring_front.isChecked()))
        lay.addWidget(self._bring_front)
        btn = QPushButton("解除每日上限（风险自负）")
        btn.setObjectName("Ghost")
        btn.clicked.connect(state.remove_daily_limit)
        lay.addWidget(btn)
        lay.addStretch(1)
        self.refresh()

    def refresh(self) -> None:
        s = self.state
        hook_detail = "抖音网页私信通道"
        if not s.hook_ok and s.hook_error:
            hook_detail = f"{hook_detail} | {s.hook_error[:160]}"
        backend_detail = "OK" if s.backend_ok else "X"
        if not s.backend_ok and s.backend_error:
            backend_detail = f"X | {s.backend_error[:120]}"
        self._status.setText(
            f"channel: {'OK' if s.hook_ok else 'X'}   {hook_detail}\n"
            f"backend: {backend_detail}\n"
            f"抖音账号 ID: {s.self_wxid or '-'}"
        )
        rc, sc = s.today_counts()
        self._counts.setText(f"今日 收 {rc} / 发 {sc}")

    def _toggle_auto(self) -> None:
        self.state.toggle_auto_send()
        self._auto.setChecked(self.state.cfg.auto_send)
