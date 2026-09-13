"""挂件统一视觉主题 —— 深海蓝客服控制台。

布局语言取自用户提供的记忆工作台参考图：深色侧栏、暖白工作区、细边框和高密度数据卡片。
所有颜色集中为令牌，旧页面沿用既有 objectName 即可随主框架统一换肤。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QFontDatabase

ACCENT = "#174F97"

QSS = """
* { font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"; font-size: 13px; color: #18263A; }
QWidget { background-color: #F3F6FA; color: #18263A; }
QLabel { background-color: transparent; color: #18263A; }
QWidget#Workbench { background: #F3F6FA; }
QWidget#Sidebar { background: #07182B; border-right: 1px solid #102B48; }
QWidget#ContentShell { background: #F3F6FA; }
QFrame#TopHeader { background: #FFFFFF; border-bottom: 1px solid #D8E0EA; }
QLabel#BrandMark { background: transparent; border: none; }
QLabel#BrandName { color: #F5F8FD; font-size: 16px; font-weight: 700; }
QLabel#BrandSub { color: #6F8CAA; font: 10px "Bahnschrift"; letter-spacing: 1px; }
QLabel#NavGroup { color: #557492; font-size: 10px; font-weight: 600; padding: 10px 8px 2px 8px; }
QLabel#SidebarFoot { color: #6F8CAA; font-size: 11px; }
QLabel#SidebarFootStrong { color: #DCE8F5; font-weight: 600; }
QLabel#HeaderTitle { color: #0D223D; font-size: 20px; font-weight: 700; }
QLabel#HeaderSub { color: #718096; font-size: 12px; }
QLabel#PageTitle { color: #0D223D; font-size: 22px; font-weight: 700; }
QLabel#SectionTitle, QLabel#Title { font-size: 15px; font-weight: 700; color: #132A46; }
QLabel#FieldLabel { color: #324861; font-size: 12px; font-weight: 600; }
QLabel#Muted { color: #728197; }
QLabel#Danger { color: #B93E4B; }
QLabel#InfoNote { background: #EAF2FF; color: #315783; border: 1px solid #C9DCF7; border-radius: 8px; padding: 9px; }

QPushButton#NavItem {
    text-align: left; min-height: 24px; padding: 9px 13px; border: none; border-radius: 8px;
    background: transparent; color: #A9BCD0; font-weight: 500;
}
QPushButton#NavItem:hover { background: #102B48; color: #F4F8FC; }
QPushButton#NavItem:checked {
    background: #F7F9FC; color: #123D70; font-weight: 700;
    border-left: 3px solid #4A82F0;
}

QFrame#HeaderStat { background: #FFFFFF; border-left: 1px solid #E0E6EE; }
QLabel#HeaderStatLabel { color: #7A8798; font-size: 11px; font-weight: 600; }
QLabel#HeaderStatValue { color: #102B4D; font: 700 18px "Bahnschrift", "Microsoft YaHei UI"; }
QFrame#StatCard { background: #FFFFFF; border: 1px solid #DCE3EC; border-radius: 10px; }
QLabel#StatLabel { color: #718096; font-size: 11px; font-weight: 600; }
QLabel#StatValue { color: #0E315C; font: 700 24px "Bahnschrift", "Microsoft YaHei UI"; }
QFrame#Toolbar, QFrame#SectionCard, QFrame#DetailPanel {
    background: #FFFFFF; border: 1px solid #DCE3EC; border-radius: 10px;
}
QFrame#DetailPanel { background: #FBFCFE; }

QListWidget, QTableWidget, QTableView, QTreeView {
    background: #FFFFFF; color: #18263A; alternate-background-color: #F8FAFD;
    border: 1px solid #DCE3EC; border-radius: 9px; padding: 3px; gridline-color: #E7ECF2;
}
QFrame#SectionCard QTableWidget { border: none; border-radius: 10px; }
QListWidget::item, QTableWidget::item, QTableView::item, QTreeView::item {
    padding: 8px 10px; color: #24364C; background-color: transparent;
}
QListWidget::item:hover, QTableWidget::item:hover { background: #F0F5FC; }
QListWidget::item:selected, QTableWidget::item:selected, QTableView::item:selected, QTreeView::item:selected {
    background: #E5EFFD; color: #123D70;
}
QHeaderView::section {
    background: #F4F7FB; color: #53657A; border: none;
    border-right: 1px solid #E3E8EF; border-bottom: 1px solid #DCE3EC;
    padding: 9px; font-size: 11px; font-weight: 600;
}

QTextEdit, QPlainTextEdit, QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox,
QDateEdit, QDateTimeEdit, QTimeEdit {
    background: #FFFFFF; color: #18263A; border: 1px solid #CBD6E2;
    border-radius: 8px; padding: 7px; selection-background-color: #BBD3F5; selection-color: #102B4D;
}
QTextEdit:focus, QPlainTextEdit:focus, QLineEdit:focus, QComboBox:focus,
QSpinBox:focus, QDoubleSpinBox:focus { border: 1px solid #2F6FED; }
QComboBox::drop-down { border: none; width: 24px; }
QComboBox QAbstractItemView { background: #FFFFFF; color: #18263A; selection-background-color: #E5EFFD; }
QScrollArea, QScrollArea > QWidget > QWidget { background: #FFFFFF; color: #18263A; border: none; }
QScrollBar:vertical { background: #EEF2F7; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #AEBFD1; min-height: 28px; border-radius: 4px; margin: 2px; }
QScrollBar::handle:vertical:hover { background: #829AB3; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QSplitter::handle { background: #E1E7EF; width: 1px; height: 1px; }

QPushButton {
    background: #FFFFFF; color: #21364E; border: 1px solid #CBD6E2;
    border-radius: 8px; padding: 7px 12px; font-weight: 500;
}
QPushButton:hover { background: #F1F5FA; border-color: #9EB3CA; }
QPushButton:pressed { background: #E4EBF3; }
QPushButton#Primary {
    background: #174F97; color: #FFFFFF; border: 1px solid #174F97;
    border-radius: 8px; padding: 8px 16px; font-weight: 700;
}
QPushButton#Primary:hover { background: #0F3F7D; border-color: #0F3F7D; }
QPushButton#Ghost { background: #F5F8FC; color: #23476E; border: 1px solid #D1DBE6; }
QPushButton#Ghost:hover { background: #E9F0F8; }
QPushButton#DangerButton { color: #AD3444; background: #FFF7F8; border: 1px solid #E8C7CC; }
QPushButton#DangerButton:hover { background: #FCEBED; }

QLabel#BubbleIn, QWidget#BubbleIn {
    background: #FFFFFF; color: #18263A; border: 1px solid #D6E0EA; border-radius: 9px; padding: 8px 12px;
}
QLabel#BubbleOut, QWidget#BubbleOut {
    background: #174F97; color: #FFFFFF; border: 1px solid #174F97; border-radius: 9px; padding: 8px 12px;
}
QWidget#ConsolePanel, QWidget#MemoryPage { background: #F3F6FA; }
QLabel#ConvName { font-weight: 700; color: #132A46; }
QLabel#ConvLast { color: #728197; font-size: 12px; }
QLabel#BadgeDouyin { background: #FDE8EC; color: #B21E35; border-radius: 7px; padding: 1px 6px; font-size: 10px; }
QLabel#PendingDot { color: #C74653; font-weight: 700; }
QLabel#AiTag { background: #DDEBFF; color: #174F97; border-radius: 6px; padding: 0 5px; font-size: 10px; }
QLabel#AiOffTip { color: #B93E4B; font-size: 12px; }
QCheckBox { color: #273B52; spacing: 7px; }
QCheckBox::indicator { width: 15px; height: 15px; }
QCheckBox::indicator:checked { background: #174F97; border: 2px solid #174F97; border-radius: 3px; }
QCheckBox:disabled, QLabel:disabled, QPushButton:disabled { color: #9AA7B5; background-color: #EEF2F6; }
QMenu { background: #FFFFFF; color: #18263A; border: 1px solid #CAD5E1; padding: 4px; }
QMenu::item { background: transparent; color: #18263A; padding: 7px 24px 7px 10px; }
QMenu::item:selected { background: #E5EFFD; color: #123D70; }
QToolTip { background: #102B4D; color: #FFFFFF; border: 1px solid #294D73; padding: 5px; }

/* LoCoMo / Mem0 workbench: light surfaces with high-contrast deep-blue typography. */
QWidget#MemoryPage { background: #F3F6FA; color: #153E68; }
QWidget#MemoryPage QLabel { color: #153E68; }
QLabel#MemoryPageTitle { color: #0D2F57; font-size: 21px; font-weight: 700; }
QLabel#MemoryMuted { color: #46627E; font-size: 11px; }
QLabel#MemoryEngineBadge {
    background: #E7F0FC; color: #174F97; border: 1px solid #BFD2E8; border-radius: 8px;
    padding: 7px 11px; font: 700 10px "Bahnschrift"; letter-spacing: 1px;
}
QFrame#MemoryStatCard {
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #FFFFFF,stop:1 #F1F6FC);
    border: 1px solid #C7D6E6; border-radius: 8px;
}
QLabel#MemoryStatLabel { color: #36597C; font-size: 11px; font-weight: 600; }
QLabel#MemoryStatValue { color: #0B2E55; font: 700 23px "Bahnschrift", "Microsoft YaHei UI"; }
QLabel#MemoryStatHint { color: #52708E; font-size: 10px; }
QFrame#MemoryToolbar, QFrame#MemoryPipeline, QFrame#MemoryPanel {
    background: #FFFFFF; border: 1px solid #CEDAE7; border-radius: 8px;
}
QFrame#MemoryPanelHeader { background: #F4F7FB; border: none; border-bottom: 1px solid #D5DFEA; }
QWidget#MemoryPipelineStep, QWidget#MemoryMessageRow { background: transparent; border: none; }
QLabel#MemoryPanelTitle { color: #123A63; font-size: 14px; font-weight: 700; }
QLabel#MemoryStageName { color: #153E68; font-size: 11px; font-weight: 600; }
QLabel#MemoryStageState { color: #55718C; font-size: 9px; }
QLabel#MemoryStageDotPending, QLabel#MemoryStageDotActive,
QLabel#MemoryStageDotDone, QLabel#MemoryStageDotReadonly {
    border-radius: 14px; font: 700 11px "Bahnschrift";
}
QLabel#MemoryStageDotPending { background: #EDF2F7; color: #496783; border: 1px solid #BDCCDB; }
QLabel#MemoryStageDotActive { background: #2F7FE8; color: #FFFFFF; border: 1px solid #74ADFF; }
QLabel#MemoryStageDotDone { background: #DCEAFE; color: #174F97; border: 1px solid #9CBCE0; }
QLabel#MemoryStageDotReadonly { background: #E7EDF3; color: #415E79; border: 1px solid #B7C5D3; }
QWidget#MemoryPage QComboBox, QWidget#MemoryPage QSpinBox, QWidget#MemoryPage QPlainTextEdit {
    background: #FFFFFF; color: #153E68; border: 1px solid #B8C9DA; border-radius: 7px;
    padding: 6px; selection-background-color: #C9DCF5;
}
QWidget#MemoryPage QComboBox:focus, QWidget#MemoryPage QSpinBox:focus,
QWidget#MemoryPage QPlainTextEdit:focus { border: 1px solid #438FE3; }
QWidget#MemoryPage QComboBox QAbstractItemView {
    background: #FFFFFF; color: #153E68; border: 1px solid #B8C9DA;
    selection-background-color: #DCEAFE;
}
QWidget#MemoryPage QCheckBox { color: #284F75; }
QWidget#MemoryPage QCheckBox::indicator:checked { background: #2F7FE8; border-color: #2F7FE8; }
QPushButton#MemoryPrimary {
    background: #216DC1; color: #FFFFFF; border: 1px solid #3E8CE0; border-radius: 7px;
    padding: 8px 16px; font-weight: 700;
}
QPushButton#MemoryPrimary:hover { background: #2B7DD5; }
QPushButton#MemoryGhost {
    background: #F5F8FC; color: #234F78; border: 1px solid #BCD0E2; border-radius: 7px;
}
QPushButton#MemoryGhost:hover { background: #E7EFF8; }
QScrollArea#MemoryMessageScroll, QScrollArea#MemoryRecallScroll,
QWidget#MemoryMessageHost, QWidget#MemoryRecallHost { background: #F7F9FC; border: none; }
QFrame#MemoryComposer { background: #FFFFFF; border: none; border-top: 1px solid #D5DFEA; }
QLabel#MemoryFieldLabel { color: #36597C; font-size: 11px; font-weight: 600; }
QFrame#MemoryBubbleAssistant {
    background: #EEF4FA; color: #153E68; border: 1px solid #C5D5E5; border-radius: 9px;
}
QFrame#MemoryBubbleUser {
    background: #DDEBFF; color: #123D70; border: 1px solid #ABC8EA; border-radius: 9px;
}
QFrame#MemoryBubbleUser QLabel { color: #123D70; }
QLabel#MemoryBubbleMeta { color: #4E6B87; font-size: 9px; font-weight: 600; }
QFrame#MemoryBubbleUser QLabel#MemoryBubbleMeta { color: #365E86; }
QLabel#MemoryBubbleText { color: #153E68; font-size: 12px; }
QLabel#MemoryResultBadge {
    background: #E3EFFD; color: #174F97; border: 1px solid #AFC9E5; border-radius: 17px;
    font: 700 11px "Bahnschrift";
}
QFrame#MemoryRecallCard { background: #FFFFFF; border: none; border-bottom: 1px solid #D5DFEA; }
QFrame#MemoryRecallCard:hover { background: #F1F6FC; }
QLabel#MemoryRecallRank { color: #496783; font: 700 10px "Bahnschrift"; }
QLabel#MemoryRecallScore { color: #174F97; font: 700 11px "Bahnschrift"; }
QLabel#MemoryRecallText { color: #153E68; font-size: 12px; }
QLabel#MemoryRecallMeta { color: #506D89; font-size: 9px; }
QLabel#MemoryEmpty { color: #506D89; padding: 28px; }
QLabel#MemoryStatus, QLabel#MemoryStatusWarning, QLabel#MemoryStatusError {
    border-radius: 6px; padding: 7px 10px; font-size: 10px;
}
QLabel#MemoryStatus { background: #E8F2FE; color: #174F97; border: 1px solid #B8D0EA; }
QLabel#MemoryStatusWarning { background: #FFF7E6; color: #79520B; border: 1px solid #E5C986; }
QLabel#MemoryStatusError { background: #FFF0F2; color: #8E2E3D; border: 1px solid #E2B8BF; }
QWidget#MemoryPage QScrollBar:vertical { background: #EDF2F7; }
QWidget#MemoryPage QScrollBar::handle:vertical { background: #9DB1C5; }
QSplitter#MemorySplitter::handle { background: #C9D5E1; width: 2px; }

/* Memory governance and operations: same deep-blue product system, denser data surfaces. */
QWidget#OpsPage { background: #F3F6FA; color: #153E68; }
QLabel#OpsTitle { color: #0D2F57; font-size: 21px; font-weight: 700; }
QLabel#OpsMuted { color: #526B84; font-size: 11px; }
QLabel#OpsStatus {
    background: #E8F2FE; color: #174F97; border: 1px solid #B8D0EA;
    border-radius: 6px; padding: 7px 10px;
}
QFrame#OpsEditor, QFrame#OpsCard {
    background: #FFFFFF; border: 1px solid #CEDAE7; border-radius: 8px;
}
QProgressBar#MemoryIngestProgress {
    min-height: 18px; background: #E3EBF4; color: #123A63;
    border: 1px solid #B8C9DA; border-radius: 6px; text-align: center;
    font-size: 10px; font-weight: 700;
}
QProgressBar#MemoryIngestProgress::chunk {
    background: #2678C8; border-radius: 5px;
}
QLabel#OpsCardValue { color: #123A63; font-size: 17px; font-weight: 700; }
QTableWidget#OpsTable {
    background: #FFFFFF; alternate-background-color: #F6F9FC; color: #183A5E;
    border: 1px solid #C9D6E4; border-radius: 8px; gridline-color: #DEE6EF;
    selection-background-color: #DCEAFE; selection-color: #123D70;
}
QTableWidget#OpsTable QHeaderView::section {
    background: #EAF1F8; color: #274E73; border: none; border-right: 1px solid #CBD8E5;
    border-bottom: 1px solid #BFCEDF; padding: 7px; font-weight: 700;
}
"""


def apply_theme(app) -> None:
    # 便携 Python/PySide6 在部分 Windows 机器上不会自动枚举系统中文字体，QSS 虽写了
    # 微软雅黑仍会回退成方块。显式注册系统字体文件；找不到时安静回落系统默认字体。
    for font_path in (
        Path(r"C:\Windows\Fonts\msyh.ttc"),
        Path(r"C:\Windows\Fonts\msyhbd.ttc"),
        Path(r"C:\Windows\Fonts\msyhl.ttc"),
    ):
        if font_path.exists():
            QFontDatabase.addApplicationFont(str(font_path))
    app.setStyleSheet(QSS)
