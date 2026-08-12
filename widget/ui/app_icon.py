"""工作台窗口、Windows 任务栏与系统托盘共用的项目图标。"""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap


def project_icon_path() -> Path:
    """返回统一品牌 SVG；旧根目录 ``icon.png`` 不再参与任何运行时图标。"""
    return (Path(__file__).resolve().parents[2] / "assets" / "branding" /
            "ai-customer-service.svg")


def windows_icon_path() -> Path:
    """返回供 Windows 快捷方式使用的多尺寸 ICO。"""
    return project_icon_path().with_suffix(".ico")


def load_app_icon() -> QIcon:
    path = project_icon_path()
    if not path.is_file():
        return QIcon()
    source = QPixmap(str(path))
    if source.isNull():
        return QIcon()
    # Windows 同时请求 16/20/24/32/40/48/64/256 等不同规格。显式加入多分辨率 pixmap，
    # 避免 pythonw 的通用 PE 图标在小任务栏尺寸下盖过单张 PNG 的惰性缩放结果。
    icon = QIcon()
    for size in (16, 20, 24, 32, 40, 48, 64, 128, 256):
        icon.addPixmap(source.scaled(
            size, size, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))
    return icon


def brand_mark_pixmap(icon: QIcon, size: int = 38) -> QPixmap:
    """侧栏品牌标记与窗口/任务栏共用同一个图标源。"""
    return icon.pixmap(int(size), int(size))


def set_windows_app_user_model_id() -> None:
    """让 Windows 任务栏把 pythonw 窗口识别为本应用，而不是 Python 通用图标。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        setter = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        setter.argtypes = [ctypes.c_wchar_p]
        setter.restype = ctypes.c_long
        # 品牌图标变更时升级 AppUserModelID，避免 Explorer 沿用旧任务栏图标缓存。
        setter("AI.Customer.Service.Workbench.BrandV2")
    except Exception:
        pass


def apply_windows_window_icon(window, icon: QIcon) -> None:
    """在顶层窗口创建 HWND 后，把 Qt 图标再次写入 Windows 的大小窗口图标槽。

    `QApplication.setWindowIcon()` 覆盖 Qt 层；这里补齐 `WM_SETICON` 与窗口类图标，专门处理
    pythonw.exe 启动时任务栏仍沿用解释器通用图标的 Windows 缓存路径。
    """
    if icon.isNull():
        return
    window.setWindowIcon(icon)
    if os.name != "nt":
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                        ctypes.c_size_t, ctypes.c_ssize_t]
        user32.SendMessageW.restype = ctypes.c_ssize_t
        user32.GetClassLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.GetClassLongPtrW.restype = ctypes.c_void_p
        user32.SetClassLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        user32.SetClassLongPtrW.restype = ctypes.c_void_p
        hwnd = int(window.winId())
        wm_geticon, wm_seticon = 0x007F, 0x0080
        icon_small, icon_big, icon_small2 = 0, 1, 2
        hbig = user32.SendMessageW(hwnd, wm_geticon, icon_big, 0)
        hsmall = user32.SendMessageW(hwnd, wm_geticon, icon_small2, 0) or \
            user32.SendMessageW(hwnd, wm_geticon, icon_small, 0)
        if hbig:
            user32.SendMessageW(hwnd, wm_seticon, icon_big, hbig)
            user32.SetClassLongPtrW(hwnd, -14, hbig)       # GCLP_HICON
        if hsmall:
            user32.SendMessageW(hwnd, wm_seticon, icon_small, hsmall)
            user32.SendMessageW(hwnd, wm_seticon, icon_small2, hsmall)
            user32.SetClassLongPtrW(hwnd, -34, hsmall)     # GCLP_HICONSM
    except Exception:
        pass


def status_icon(base: QIcon, color: str) -> QIcon:
    """保留统一 AI 品牌主图，仅在右下角叠加一个小状态点。"""
    pixmap = base.pixmap(64, 64)
    if pixmap.isNull():
        return base
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor("white"), 3))
    painter.setBrush(QColor(color))
    painter.drawEllipse(45, 45, 16, 16)
    painter.end()
    return QIcon(pixmap)
