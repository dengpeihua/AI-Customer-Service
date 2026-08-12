"""版本页：微信 / 企微各一行 —— 检测到的版本 + 可注入/需降级 + 「强制降级」按钮。

按钮只在**不可注入**时才亮；点了先弹确认（会关客户端、要重新扫码、要管理员），
确认后降级在**后台线程**里跑（安装器是分钟级的，绝不能卡住 Qt GUI 线程），
过程/结果经 Qt 信号排队回 GUI 线程刷新界面 —— 与 broadcast_page 的套路一致。

依赖全部注入，页面自己不 import widget.version_check / widget.downgrade：
  status_fn()            -> dict，形如 widget.version_check.status()：
      {"wechat": {"installed": True, "version": "4.1.11.30",
                  "required": "4.1.10.27", "injectable": False},
       "wecom":  {...}}
      （version 也接受 detected/actual，required 也接受 expected/required_version，
        injectable 也接受 ok/can_inject —— 上游改名不至于把页面打瞎。）
  downgrade_wechat_fn()  -> 无返回即成功，抛异常即失败；可选接一个 progress(str) 形参
  downgrade_wecom_fn()   -> 同上
  confirm_fn(title,text) -> bool（默认走 QMessageBox.question；测试注入以绕开模态）
  executor(fn)           -> 跑 fn 的方式（默认起 daemon 线程；测试可注入同步执行）
"""
from __future__ import annotations

import inspect
import threading
from typing import Callable, Dict, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton,
                               QVBoxLayout, QWidget)

PLATFORMS = ("wechat", "wecom")
_LABEL = {"wechat": "微信", "wecom": "企业微信"}

_CONFIRM_TITLE = "强制降级 {name}？"
_CONFIRM_TEXT = (
    "即将把{name}强制降级到可注入版本 {required}。\n\n"
    "⚠️ 这会先关闭正在登录的{name}客户端，未读消息会中断；\n"
    "⚠️ 降级完成后需要重新扫码登录；\n"
    "⚠️ 安装过程需要管理员权限，可能弹出 UAC 授权框；\n"
    "⚠️ 过程可能持续几分钟，期间请不要手动开{name}。\n\n"
    "确认继续吗？"
)


# version_check 读不到时会回这些哨兵值（NOT_FOUND="not found"）——一律当"未安装"，不当版本号
_ABSENT = {"", "-", "not found", "not_found", "notfound", "none", "unknown", "未知"}


def _pick(entry: dict, *keys, default=None):
    for k in keys:
        if k in entry and entry[k] not in (None, ""):
            return entry[k]
    return default


def normalize_entry(entry: Optional[dict]) -> dict:
    """把 status() 的一行归一成 {installed, version, required, injectable}。"""
    if not isinstance(entry, dict):
        return {"installed": False, "version": "", "required": "", "injectable": False}
    ver = str(_pick(entry, "version", "detected", "actual", default="") or "").strip()
    if ver.lower() in _ABSENT:
        ver = ""
    installed = _pick(entry, "installed", "present", default=None)
    return {
        "installed": bool(ver) if installed is None else bool(installed),
        "version": ver,
        "required": str(_pick(entry, "required", "expected", "required_version", default="") or ""),
        "injectable": bool(_pick(entry, "injectable", "ok", "can_inject", default=False)),
    }


def describe(entry: dict) -> str:
    """一行人话：'企业微信 5.1.2.9999（需降级到 5.0.3.6005）'。"""
    e = normalize_entry(entry)
    if not e["version"]:
        return "未安装（没检测到客户端）" if not e["installed"] else "已安装但读不出版本号"
    ver = e["version"]
    if e["injectable"]:
        return f"{ver} —— 可注入"
    req = e["required"] or "可注入版本"
    return f"{ver} —— 需降级到 {req}"


def _call_with_optional_progress(fn: Callable, progress: Callable[[str], None]) -> None:
    """降级函数可以选择接一个 progress 回调；不接就直接调。"""
    try:
        params = list(inspect.signature(fn).parameters.values())
        wants = any(p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) and
                    p.default is p.empty for p in params)
    except (TypeError, ValueError):
        wants = False
    if wants:
        fn(progress)
    else:
        fn()


class VersionPage(QWidget):
    _progress_sig = Signal(str, str)          # platform, 文案
    _finished_sig = Signal(str, bool, str)    # platform, ok, 文案

    def __init__(self, status_fn: Callable[[], dict],
                 downgrade_wechat_fn: Callable,
                 downgrade_wecom_fn: Callable,
                 confirm_fn: Optional[Callable[[str, str], bool]] = None,
                 log: Optional[Callable[[str], None]] = None,
                 executor: Optional[Callable[[Callable[[], None]], None]] = None):
        super().__init__()
        self._status_fn = status_fn
        self._fns = {"wechat": downgrade_wechat_fn, "wecom": downgrade_wecom_fn}
        self._confirm = confirm_fn or self._default_confirm
        self._log = log or (lambda msg: None)
        self._executor = executor or self._default_executor
        self._status: Dict[str, dict] = {p: normalize_entry(None) for p in PLATFORMS}
        self._busy: Dict[str, bool] = {p: False for p in PLATFORMS}
        self._version_lbl: Dict[str, QLabel] = {}
        self._result_lbl: Dict[str, QLabel] = {}
        self._btn: Dict[str, QPushButton] = {}

        self._progress_sig.connect(self._on_progress)
        self._finished_sig.connect(self._on_finished)

        lay = QVBoxLayout(self)
        t = QLabel("版本")
        t.setObjectName("Title")
        lay.addWidget(t)
        tip = QLabel("挂件只能注入指定版本的客户端。版本不对时可强制降级——会关客户端、需重新扫码、需管理员。")
        tip.setObjectName("Muted")
        tip.setWordWrap(True)
        lay.addWidget(tip)
        for p in PLATFORMS:
            lay.addWidget(self._build_row(p))
        lay.addStretch(1)
        self.refresh()

    # ---------- UI ----------
    def _build_row(self, platform: str) -> QWidget:
        box = QFrame()
        box.setObjectName("Card")
        outer = QVBoxLayout(box)
        row = QHBoxLayout()
        name = QLabel(_LABEL[platform])
        name.setMinimumWidth(72)
        row.addWidget(name)
        ver = QLabel("未知")
        ver.setWordWrap(True)
        self._version_lbl[platform] = ver
        row.addWidget(ver, 1)
        btn = QPushButton("强制降级")
        btn.setObjectName("Ghost")
        btn.setEnabled(False)
        btn.clicked.connect(lambda _=False, pf=platform: self.request_downgrade(pf))
        self._btn[platform] = btn
        row.addWidget(btn)
        outer.addLayout(row)
        res = QLabel("")
        res.setObjectName("Muted")
        res.setWordWrap(True)
        self._result_lbl[platform] = res
        outer.addWidget(res)
        return box

    def refresh(self) -> None:
        try:
            raw = self._status_fn() or {}
        except Exception as exc:                     # 注册表/文件读不到也绝不能崩挂件
            self._log(f"[version] status() 失败: {exc}")
            raw = {}
        for p in PLATFORMS:
            entry = normalize_entry(raw.get(p) if isinstance(raw, dict) else None)
            self._status[p] = entry
            self._version_lbl[p].setText(describe(entry) if raw else "未知")
            self._btn[p].setEnabled(self._should_enable(p))

    def _should_enable(self, platform: str) -> bool:
        if self._busy[platform]:
            return False
        e = self._status[platform]
        return bool(e["version"]) and not e["injectable"]

    # ---------- 降级 ----------
    def request_downgrade(self, platform: str) -> None:
        if self._busy.get(platform):
            return
        if not self._should_enable(platform):
            return
        name = _LABEL[platform]
        req = self._status[platform]["required"] or "可注入版本"
        try:
            ok = bool(self._confirm(_CONFIRM_TITLE.format(name=name),
                                    _CONFIRM_TEXT.format(name=name, required=req)))
        except Exception as exc:
            self._log(f"[version] 确认框异常: {exc}")
            ok = False
        if not ok:
            self._result_lbl[platform].setText("已取消，未做任何改动")
            return
        self._busy[platform] = True
        self._btn[platform].setEnabled(False)
        self._result_lbl[platform].setText(f"{name}降级进行中…请不要关闭挂件")
        self._log(f"[version] 开始强制降级 {platform} -> {req}")
        fn = self._fns[platform]

        def worker() -> None:
            try:
                _call_with_optional_progress(
                    fn, lambda msg, pf=platform: self._progress_sig.emit(pf, str(msg)))
                self._finished_sig.emit(platform, True, f"{name}降级成功")
            except Exception as exc:                  # 后台线程里绝不让异常逃逸
                self._finished_sig.emit(platform, False, f"{name}降级失败：{exc}")

        try:
            self._executor(worker)
        except Exception as exc:
            self._log(f"[version] 起降级任务失败: {exc}")
            self._on_finished(platform, False, f"{name}降级失败：{exc}")

    @staticmethod
    def _default_executor(fn: Callable[[], None]) -> None:
        threading.Thread(target=fn, daemon=True, name="force-downgrade").start()

    def _default_confirm(self, title: str, text: str) -> bool:
        return QMessageBox.question(self, title, text,
                                    QMessageBox.Yes | QMessageBox.No,
                                    QMessageBox.No) == QMessageBox.Yes

    # ---------- 信号槽（GUI 线程）----------
    def _on_progress(self, platform: str, text: str) -> None:
        if platform in self._result_lbl:
            self._result_lbl[platform].setText(text)

    def _on_finished(self, platform: str, ok: bool, text: str) -> None:
        self._busy[platform] = False
        self._result_lbl[platform].setText(text)
        self._log(f"[version] {platform} 降级{'成功' if ok else '失败'}: {text}")
        self.refresh()

    # ---------- 测试/外部可用的薄接口 ----------
    def button(self, platform: str) -> QPushButton:
        return self._btn[platform]

    def is_downgrade_enabled(self, platform: str) -> bool:
        return self._btn[platform].isEnabled()

    def version_text(self, platform: str) -> str:
        return self._version_lbl[platform].text()

    def result_text(self, platform: str) -> str:
        return self._result_lbl[platform].text()

    def is_busy(self, platform: str) -> bool:
        return bool(self._busy.get(platform))
