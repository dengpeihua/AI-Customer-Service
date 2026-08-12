"""多开器 Python 封装 —— 调 native/build/out/multiopen.exe 绕单实例互斥拉起第 N 个客户端。

跨产品（微信 + 企微），故放 widget 根而非 wecom 子包。真机发现（native/multiopen/DISCOVERY-NOTES.md）：
- **企微**强制单实例 → 关 `Tencent.WeWork.ExclusiveObject` 才能多开（close=True + mutex_substr）。
- **微信 4.x 原生多开** → 裸启动即成，无需关互斥（close=False，或 launch-only）。

镜像 wecom/connector._default_injector_run 的调用手法（capture_output/CREATE_NO_WINDOW/timeout），
但**解析 stdout 的一行 JSON** 取 new_pid（injector wrapper 丢弃了 stdout，这里需要它）。
best-effort：任何失败返回 None，**绝不抛进 GUI**。
"""
from __future__ import annotations

import json
import subprocess
from typing import Callable, Optional

CREATE_NO_WINDOW = 0x08000000


def parse_new_pid(stdout: str) -> Optional[int]:
    """从 multiopen stdout 取**最后一行**合法 JSON 的 new_pid（>0）。解析不出/为 0 返回 None。

    multiopen 的 close/launch 模式 stdout 只有一行 JSON，如
    {"ok":true,"target":"WXWork.exe","instances":1,"closed":2,"new_pid":30400}。
    """
    for line in reversed([ln for ln in stdout.splitlines() if ln.strip()]):
        try:
            d = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(d, dict):
            continue
        pid = d.get("new_pid")
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return None
        return pid if pid > 0 else None
    return None


def _default_runner(argv: list[str], timeout_s: float):
    """默认执行器：跑 multiopen.exe，返回 (returncode, stdout, stderr)。不开窗。"""
    proc = subprocess.run(argv, capture_output=True, text=True,
                          timeout=timeout_s, creationflags=CREATE_NO_WINDOW)
    return proc.returncode, proc.stdout, proc.stderr


def launch_new_instance(
    exe_path: str,
    process_name: str,
    *,
    launch_path: Optional[str] = None,
    launch_wechat: bool = False,
    launch_wecom: bool = False,
    mutex_substr: Optional[str] = None,
    close: bool = True,
    timeout_s: float = 40.0,
    log: Optional[Callable[[str], None]] = None,
    runner: Optional[Callable[[list[str], float], tuple]] = None,
) -> Optional[int]:
    """拉起 process_name 的一个新实例，返回新 pid；失败返回 None（不抛）。

    企微：`launch_new_instance(exe, "WXWork.exe", launch_wecom=True, mutex_substr="ExclusiveObject")`。
    微信：`launch_new_instance(exe, "Weixin.exe", launch_wechat=True, close=False)`（原生多开，不关互斥）。
    """
    argv = [exe_path, "--process", process_name]
    if close and mutex_substr:
        argv += ["--mutex-substr", mutex_substr]
    if not close:
        argv += ["--no-close"]
    if launch_path:
        argv += ["--launch", launch_path]
    elif launch_wechat:
        argv += ["--launch-wechat"]
    elif launch_wecom:
        argv += ["--launch-wecom"]

    run = runner or _default_runner
    try:
        rc, out, err = run(argv, timeout_s)
    except Exception as e:                       # noqa: BLE001 —— 多开失败绝不崩挂件
        if log:
            log(f"[multiopen] 运行失败：{e}")
        return None

    if log and err:
        for ln in err.splitlines():
            s = ln.strip()
            if s:
                log(s if s.startswith("[multiopen]") else f"[multiopen] {s}")
    if rc != 0 and log:
        log(f"[multiopen] 退出码 {rc}"
            "（0 ok/2 用法或未找到/4 权限/5 CreateProcess 失败/6 未关成互斥/7 ntdll 不可用）")

    # new_pid 以 stdout 的 JSON 为准（即便 rc 非 0，只要真拉起了就有 pid）。
    return parse_new_pid(out)
