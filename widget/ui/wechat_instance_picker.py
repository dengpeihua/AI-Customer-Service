"""启动时选择要接管的个人微信进程。"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from widget.wechat.selection import WechatProcessChoice, focus_wechat_process


class WechatInstancePicker(QDialog):
    def __init__(self, choices: list[WechatProcessChoice], *,
                 focus_fn: Optional[Callable[[int], object]] = None, parent=None):
        super().__init__(parent)
        self.setObjectName("WechatInstancePicker")
        self.setWindowTitle("选择要接管的个人微信")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setMinimumWidth(590)
        self._choices = list(choices)
        self._focus_fn = focus_fn or focus_wechat_process
        self._group = QButtonGroup(self)
        self._radios: dict[int, QRadioButton] = {}

        layout = QVBoxLayout(self)
        title = QLabel("检测到多个已登录微信，请选择 AI 客服要接管的账号")
        title.setObjectName("Title")
        layout.addWidget(title)
        intro = QLabel("不确定账号时，先点“查看窗口”，确认微信内容后再回来选择。")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self._preview_status = QLabel("")
        self._preview_status.setObjectName("InfoNote")
        self._preview_status.setWordWrap(True)
        self._preview_status.hide()
        layout.addWidget(self._preview_status)

        for choice in self._choices:
            row = QWidget(self)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(8, 6, 8, 6)
            radio = QRadioButton(
                f"{choice.account_label}  ·  PID {choice.pid}  ·  "
                f"端口 {choice.hook_port or '识别中'}  ·  窗口“{choice.window_title}”",
                row,
            )
            radio.setObjectName("WechatChoiceRadio")
            self._group.addButton(radio, choice.pid)
            self._radios[choice.pid] = radio
            row_layout.addWidget(radio, 1)
            preview = QPushButton("查看窗口", row)
            preview.setObjectName("Ghost")
            preview.clicked.connect(lambda _checked=False, pid=choice.pid: self.preview_pid(pid))
            row_layout.addWidget(preview)
            layout.addWidget(row)

        warning = QLabel(
            "没被选中的微信会始终保持在线。确认后只接管所选账号；每次连接都会核对监听端口与"
            "微信 PID，避免会话或回复串到另一个账号。"
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("color:#b45309;")
        layout.addWidget(warning)

        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel = QPushButton("暂不接管", self)
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        take = QPushButton("接管所选微信", self)
        take.setObjectName("Primary")
        take.clicked.connect(self._accept_selected)
        actions.addWidget(take)
        layout.addLayout(actions)

    def preview_pid(self, pid: int) -> None:
        # “查看窗口”同时选中对应账号，让用户确认后无需再猜是哪一行。选择器本身原本是
        # always-on-top；若不先最小化，即使微信成功获得焦点也仍被它盖住，看起来就像按钮失效。
        self.select_pid(pid)
        self._preview_status.setText(
            "正在查看所选微信；确认账号后，请从任务栏打开“选择要接管的个人微信”返回。"
        )
        self._preview_status.show()
        self.showMinimized()
        QApplication.processEvents()
        focused = self._focus_fn(int(pid))
        if focused is False:
            self.showNormal()
            self.raise_()
            self.activateWindow()
            self._preview_status.setText(
                "未能自动显示该微信，请点击任务栏中的微信图标确认账号；选择状态已经保留。"
            )

    def preview_status(self) -> str:
        return self._preview_status.text()

    def select_pid(self, pid: int) -> None:
        radio = self._radios.get(int(pid))
        if radio is not None:
            radio.setChecked(True)

    def selected_choice(self) -> Optional[WechatProcessChoice]:
        selected_pid = self._group.checkedId()
        return next((choice for choice in self._choices if choice.pid == selected_pid), None)

    def choice_count(self) -> int:
        return len(self._choices)

    def _accept_selected(self) -> None:
        if self.selected_choice() is None:
            QMessageBox.information(self, "选择个人微信", "请先选择一个要接管的微信账号")
            return
        self.accept()


def choose_wechat_instance(
    choices: list[WechatProcessChoice], *, parent=None,
    focus_fn: Optional[Callable[[int], object]] = None,
) -> Optional[WechatProcessChoice]:
    dialog = WechatInstancePicker(choices, parent=parent, focus_fn=focus_fn)
    return dialog.selected_choice() if dialog.exec() == QDialog.DialogCode.Accepted else None
