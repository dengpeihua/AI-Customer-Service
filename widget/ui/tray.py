from __future__ import annotations
from typing import Callable
from PySide6.QtGui import QIcon, QAction
from PySide6.QtWidgets import QSystemTrayIcon, QMenu
from widget.ui.app_icon import load_app_icon, status_icon

class Tray:
    def __init__(self, state, on_open: Callable[[], None], on_quit: Callable[[], None],
                  on_handoff: Callable[[], None] | None = None,
                  app_icon: QIcon | None = None):
        self.state = state
        self._on_open = on_open
        self._base_icon = app_icon or load_app_icon()
        self.icon = QSystemTrayIcon(status_icon(self._base_icon, "#21A366"))
        self.icon.setToolTip("AI客服")
        # 菜单与菜单项必须挂在 self 上留住 Python 引用：否则 __init__ 返回后会被 GC，
        # QSystemTrayIcon 不持有其所有权 → 右键弹不出菜单（表现为"右键没反应、退不掉"）。
        self._menu = QMenu()
        self._actions: list[QAction] = []
        self._add_action("打开面板", on_open)
        if on_handoff is not None:
            self._add_action("转人工待处理", on_handoff)
        self._add_action("退出", on_quit)
        self.icon.setContextMenu(self._menu)
        self.icon.activated.connect(self._on_activated)

    def _add_action(self, text: str, slot: Callable[[], None]) -> None:
        act = QAction(text, self._menu)
        act.triggered.connect(slot)
        self._menu.addAction(act)
        self._actions.append(act)

    def _on_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self._on_open()

    def show(self) -> None:
        self.icon.show()

    def set_health(self, ok: bool, degraded: bool = False) -> None:
        color = "#E0A100" if degraded else ("#21A366" if ok else "#D64545")
        self.icon.setIcon(status_icon(self._base_icon, color))

    def notify(self, title: str, msg: str) -> None:
        self.icon.showMessage(title, msg)
