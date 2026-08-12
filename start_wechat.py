"""干净启动微信：先清崩溃残留（防『上次异常退出，是否修复』弹窗），再启动 Weixin.exe。

产品化用法：把微信的启动入口换成这个脚本——开机自启项 / 桌面快捷方式指向
    pythonw.exe start_wechat.py
（工作目录设为本项目根、用项目 venv 的 pythonw）。这样客户每次开微信前都自动清一遍
崩溃记录，永远看不到修复提示。清理只删崩溃诊断文件，不碰任何账号数据。

M4 多开：`pythonw.exe start_wechat.py --port 30002` 让这个实例的 hook 绑 30002
（不带 --port 时行为与以前完全一致，hook 用缺省 30001）。
"""
from __future__ import annotations

import subprocess
import sys
from typing import Optional

from widget.wechat.launcher import launch_wechat_instance, wechat_exe
from widget.wechat_hygiene import clear_crash_reports


def parse_port(argv: list[str]) -> Optional[int]:
    """从命令行取 --port（支持 `--port 30002` 与 `--port=30002`）；没有/非法返回 None。"""
    for i, arg in enumerate(argv):
        raw = None
        if arg == "--port" and i + 1 < len(argv):
            raw = argv[i + 1]
        elif arg.startswith("--port="):
            raw = arg.split("=", 1)[1]
        if raw is not None:
            try:
                return int(raw)
            except ValueError:
                return None
    return None


def main() -> None:
    port = parse_port(sys.argv)
    if port is not None:
        # --port = 作为**第一个**实例用 StartPort=<port> 起（多号部署里 primary 用 30002，
        # 把默认 30001 让给后续无参数实例）。StartPort= 只有第一个实例吃得下，须先于其它微信起。
        pid = launch_wechat_instance(start_port=port, hook_port=port, log=print)
        if pid is None:
            print(f"[start_wechat] 启动微信失败（端口={port}），详见上面的日志")
            return
        print(f"[start_wechat] 已启动微信 pid={pid} 端口={port}")
        return
    n = clear_crash_reports()
    print(f"[start_wechat] 已清崩溃残留 {n} 个文件")
    exe = wechat_exe()
    print(f"[start_wechat] 启动微信：{exe}")
    subprocess.Popen([exe])


if __name__ == "__main__":
    main()
