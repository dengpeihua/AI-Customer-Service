"""启动微信前清掉崩溃残留，让『微信上次异常退出，是否修复』弹窗永不出现（客户绝不能看到）。

微信 4.x 用 Crashpad 处理崩溃：崩溃时把 dump 写进
`%APPDATA%\\Tencent\\xwechat\\crashinfo\\reports\\*.dmp`，记录写进 `records\\`；
微信**下次启动据这里有没有未处理的崩溃记录**判定"上次崩了" → 弹修复框（崩多了更会弹）。

在启动微信**之前**把这些崩溃记录清掉，微信就以为上次是干净退出、不弹框。
**只删崩溃诊断文件（dump/记录），绝不碰任何账号数据、消息库、配置、settings.dat**，因此安全。
"""
from __future__ import annotations

import os
from pathlib import Path

# crashinfo 下这几个子目录装的是"未处理崩溃记录"，清空即视为"无崩溃待处理"。
# 不动 settings.dat（crashpad 客户端身份）与 attachments。
_CRASH_SUBDIRS = ("reports", "records")


def crashinfo_dir(appdata: str | None = None) -> Path:
    """微信 crashpad 崩溃信息目录。appdata 可注入，缺省取 %APPDATA%。"""
    base = appdata if appdata is not None else os.environ.get("APPDATA", "")
    return Path(base) / "Tencent" / "xwechat" / "crashinfo"


def clear_crash_reports(appdata: str | None = None) -> int:
    """删掉 crashinfo 下 reports/records 里的崩溃记录文件（递归，仅文件、保留目录）。

    返回删除的文件数。全程异常安全：目录不存在 → 返回 0；个别文件被占用删不掉 → 跳过。
    只在 crashinfo 这一个目录里动手，绝不触碰其外的任何文件。
    """
    ci = crashinfo_dir(appdata)
    removed = 0
    for sub in _CRASH_SUBDIRS:
        d = ci / sub
        if not d.is_dir():
            continue
        for f in d.rglob("*"):
            if f.is_file():
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass          # 被占用/权限 → 跳过，不影响其余
    return removed
