from __future__ import annotations

from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                               QListWidget, QWidget)


def _heading(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setStyleSheet("font-weight: bold; margin-top: 8px;")
    return lab


def _chips(tags: list) -> QWidget:
    host = QWidget()
    row = QHBoxLayout(host); row.setContentsMargins(0, 0, 0, 0); row.setSpacing(6)
    for t in tags:
        chip = QLabel(str(t))
        chip.setStyleSheet("color: white; background: #12B3A6; border-radius: 8px; padding: 2px 8px;")
        row.addWidget(chip)
    row.addStretch(1)
    return host


class CustomerProfileDialog(QDialog):
    def __init__(self, profile: dict, display_name: str = "", parent=None):
        super().__init__(parent)
        name = display_name or profile.get("contact_id", "")
        self.setWindowTitle(f"客户资料 · {name}")
        self.resize(360, 480)
        lay = QVBoxLayout(self)

        lay.addWidget(_heading("画像摘要"))
        summ = QLabel(profile.get("summary") or "暂无画像，随对话自动积累")
        summ.setWordWrap(True)
        lay.addWidget(summ)

        lay.addWidget(_heading("标签"))
        tags = profile.get("tags") or []
        lay.addWidget(_chips(tags) if tags else QLabel("—"))

        lay.addWidget(_heading("最近提问"))
        ql = QListWidget()
        questions = profile.get("recent_questions") or []
        for q in questions:
            ql.addItem(str(q))
        if not questions:
            ql.addItem("（暂无）")
        lay.addWidget(ql, 1)
