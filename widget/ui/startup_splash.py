"""启动期可见反馈：耗时连接在工作台构造前完成时，先让用户看到程序已经启动。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QSplashScreen


class StartupSplash(QSplashScreen):
    """轻量启动窗口；只呈现进度，不执行任何抖音私信或网络操作。"""

    def __init__(self, icon: QIcon) -> None:
        canvas = QPixmap(520, 260)
        canvas.fill(QColor("#07182B"))
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not icon.isNull():
            painter.drawPixmap(42, 42, icon.pixmap(58, 58))
        painter.setPen(QColor("#F5F8FD"))
        title_font = QFont("Microsoft YaHei UI", 20)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.drawText(120, 78, "AI 客服工作台")
        painter.setPen(QColor("#89A4C1"))
        painter.setFont(QFont("Microsoft YaHei UI", 10))
        painter.drawText(122, 102, "正在安全接入已登录账号")
        painter.setPen(QColor("#173858"))
        painter.drawLine(42, 132, 478, 132)
        painter.end()

        # 不强制置顶：账号选择器是用户必须操作的模态窗口，必须能自然盖在启动页之上。
        super().__init__(canvas, Qt.WindowType.SplashScreen)
        self.setWindowTitle("AI客服")
        self.setWindowIcon(icon)
        self.stage = ""
        self.set_stage("正在准备运行环境…")

    def set_stage(self, message: str) -> None:
        self.stage = str(message)
        self.showMessage(
            self.stage,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
            QColor("#DCE8F5"),
        )
        app = QApplication.instance()
        if app is not None:
            app.processEvents()
