# widget/version_check.py
"""版本闸：检测本机装的微信 / 企业微信版本，以及是否是 hook 能注入的那一版。

为什么要有这个模块：hook（version.dll 劫持 + 逆向偏移）是【强绑定版本】的——微信必须
4.1.10.27、企微必须 5.0.3.6005，差一位偏移就全错（企微侧 `offsets.verify_version`
strict 精确匹配，见 widget/wecom/offsets.py）。客户机上微信/企微一自动更新，挂件就"接不上"
却看不出所以然。这里把"装的是哪一版 / 能不能注入"变成可查询的结构化状态，给 GUI 版本页
和启动器共用。

设计铁律：
- **轻依赖**：不 import 主程序模块（不拖 GUI/流水线进来），启动早期任何地方都能 import。
- **绝不抛**：读注册表/读 PE 版本全部 best-effort，失败返回空串或 NOT_FOUND，由上层决定怎么显示。
- **可测**：exe 解析器与版本读取器都是模块级函数/可注入参数，测试注入假件，永不碰真 exe / 真注册表。
- **单一事实源**：企微版本直接取 `WeComHookConfig.required_version`，不再抄第二份常量。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional

# 企微可注入版本 = 连接器强绑定的那一版（单一事实源，别再抄）
try:
    from widget.wecom.config import WeComHookConfig
    WECOM_INJECTABLE_VERSION: str = WeComHookConfig.required_version
except Exception:                                    # pragma: no cover - 兜底，配置模块异常也不能让版本页崩
    WECOM_INJECTABLE_VERSION = "5.0.3.6005"

# 微信可注入版本：此前散落在打包脚本/文档里（assets\wechat\WeChatSetup_4.1.10.27.exe），
# 挂件侧没有常量 → 在这里立一个，后续代码都引这里。
WECHAT_INJECTABLE_VERSION: str = "4.1.10.27"

NOT_FOUND = "not found"                              # 压根没找到 exe（没装/装在非常规位置）

_WECHAT_EXE_ENV = "ACS_WECHAT_EXE"
_WECOM_EXE_ENV = "ACS_WECOM_EXE"

# 企微注册表候选（对齐 customer_docs/scripts/Start-AICustomerService.ps1 的 Get-WeComInstallDir）
_WECOM_REG_KEYS: tuple[tuple[str, str], ...] = (
    ("HKCU", r"Software\Tencent\WXWork"),
    ("HKLM", r"SOFTWARE\WOW6432Node\Tencent\WXWork"),
    ("HKLM", r"SOFTWARE\Tencent\WXWork"),
)
_WECOM_FALLBACK_DIRS: tuple[str, ...] = (
    r"C:\Program Files (x86)\WXWork",
    r"C:\Program Files\WXWork",
)
_WECOM_EXE_NAME = "WXWork.exe"


# ---------------------------------------------------------------- 版本读取

def _file_version(path: str) -> str:
    """读 PE 的 VS_FIXEDFILEINFO 版本号（与 widget/app.py detect_wechat_version 同一手法）。
    复用 widget.wecom.winproc.file_version —— 读不到返回空串，绝不抛。"""
    from widget.wecom.winproc import file_version
    return file_version(path)


def _safe_version(exe_path: str) -> str:
    try:
        return (_file_version(exe_path) or "").strip()
    except Exception:
        return ""


# ---------------------------------------------------------------- exe 解析

def wechat_exe() -> str:
    """微信 exe：环境变量覆盖 > 注册表 InstallPath > 默认安装路径（复用 launcher）。找不到返回空串。"""
    env = os.environ.get(_WECHAT_EXE_ENV, "").strip()
    if env:
        return env
    try:
        from widget.wechat.launcher import wechat_exe as _launcher_wechat_exe
        return (_launcher_wechat_exe() or "").strip()
    except Exception:
        return ""


def _reg_install_path(hive: str, subkey: str) -> str:
    """读注册表 `<hive>\\<subkey>\\InstallPath`。读不到/非 Windows → 空串。"""
    try:
        import winreg
    except Exception:
        return ""
    root = winreg.HKEY_CURRENT_USER if hive == "HKCU" else winreg.HKEY_LOCAL_MACHINE
    try:
        with winreg.OpenKey(root, subkey) as key:
            return str(winreg.QueryValueEx(key, "InstallPath")[0] or "")
    except Exception:
        return ""


def wecom_exe(reg_read: Optional[Callable[[str, str], str]] = None,
              exists: Optional[Callable[[str], bool]] = None) -> str:
    """企微 exe：环境变量覆盖 > HKCU/HKLM(含 WOW6432Node) InstallPath > Program Files 回落。
    全找不到返回空串。`reg_read`/`exists` 是测试注入缝（默认真注册表 + os.path.exists）。"""
    env = os.environ.get(_WECOM_EXE_ENV, "").strip()
    if env:
        return env
    read = reg_read or _reg_install_path
    is_file = exists or os.path.exists
    for hive, subkey in _WECOM_REG_KEYS:
        try:
            base = (read(hive, subkey) or "").strip()
        except Exception:
            base = ""
        if not base:
            continue
        exe = str(Path(base) / _WECOM_EXE_NAME)
        try:
            if is_file(exe):
                return exe
        except Exception:
            continue
    for folder in _WECOM_FALLBACK_DIRS:
        exe = str(Path(folder) / _WECOM_EXE_NAME)
        try:
            if is_file(exe):
                return exe
        except Exception:
            continue
    return ""


# ---------------------------------------------------------------- 版本查询

def wechat_version(exe_path: Optional[str] = None) -> str:
    """微信版本号（如 '4.1.10.27'）。exe 找不到 → NOT_FOUND；读不出版本 → 空串。绝不抛。"""
    path = (exe_path or "").strip() or wechat_exe()
    if not path:
        return NOT_FOUND
    return _safe_version(path)


def wecom_version(exe_path: Optional[str] = None) -> str:
    """企微版本号（如 '5.0.3.6005'）。exe 找不到 → NOT_FOUND；读不出版本 → 空串。绝不抛。"""
    path = (exe_path or "").strip() or wecom_exe()
    if not path:
        return NOT_FOUND
    return _safe_version(path)


# ---------------------------------------------------------------- 可注入判定

def is_wechat_injectable(ver: Optional[str]) -> bool:
    """精确匹配：差一位（4.1.10.28）或读不到（''/'not found'）都算不可注入。"""
    return (ver or "").strip() == WECHAT_INJECTABLE_VERSION


def is_wecom_injectable(ver: Optional[str]) -> bool:
    """精确匹配（与 offsets.verify_version strict 一致）。"""
    return (ver or "").strip() == WECOM_INJECTABLE_VERSION


# ---------------------------------------------------------------- 汇总（给 UI）

def _one(resolver: Callable[[], str], reader: Callable[[Optional[str]], str],
         expected: str, judge: Callable[[Optional[str]], bool]) -> dict:
    try:
        exe = (resolver() or "").strip()
    except Exception:
        exe = ""
    ver = reader(exe) if exe else NOT_FOUND
    return {"exe": exe, "version": ver, "expected": expected, "injectable": judge(ver)}


def status() -> dict:
    """给版本页/启动器用的整体状态：
    {"wechat": {exe, version, expected, injectable}, "wecom": {...}}。绝不抛。"""
    return {
        "wechat": _one(wechat_exe, wechat_version, WECHAT_INJECTABLE_VERSION, is_wechat_injectable),
        "wecom": _one(wecom_exe, wecom_version, WECOM_INJECTABLE_VERSION, is_wecom_injectable),
    }
