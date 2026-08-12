# widget/wechat/launcher.py
"""挂件自管拉起微信实例（多号）。

★真机 2026-08-03 铁律（曾误判为「硬上限 2 个进程」，实为拉起机制）★：
- 注入的 hook（version.dll 劫持）读宿主命令行 `StartPort=<port>` 取端口——但 **`StartPort=`
  只有第一个实例吃得下**。已有微信在跑时再带 `StartPort=` 拉起，进程 0.5s 内 `0xFFFFFFFF`
  自杀（与端口是否空闲无关）。后续实例必须【无参数】起，hook 落默认 30001。
- 无参数实例**不自动递增端口**，永远盯 30001；两个无参数会撞车。
- ⇒ 能并存的两个号唯一干净组合 = **第一个 `StartPort=30002` + 第二个无参数→30001**。
  挂件按此顺序自管拉起（`launch_wechat_fleet`）。这套机制实测只容 2 个不同端口
  （`MAX_WECHAT_INSTANCES=2`）。

不走 multiopen.exe（那条子进程命令行写死在 C 里，塞不进 StartPort，且微信原生就能起第二个）。

安全：hook 服务绑 0.0.0.0，多开后暴露面 ×N —— 每拉起一个实例就同步加一条 Windows
防火墙入站拒绝规则（挡 hook 的**实际落点**端口）。加规则需管理员，失败不阻塞拉起
（不能把功能卡死），但必须记日志可见。
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Optional

CREATE_NO_WINDOW = 0x08000000
_DEFAULT_EXE = r"C:\Program Files\Tencent\Weixin\Weixin.exe"
FIREWALL_RULE_PREFIX = "AI-CS-WeChat-hook"

DEFAULT_HOOK_PORT = 30001        # 无参数实例的 hook 落点
FIRST_INSTANCE_PORT = 30002      # 第一个实例用 StartPort 挪离 30001，把 30001 让给后续实例
MAX_WECHAT_INSTANCES = 2         # 真机确认这套机制只容 2 个不同端口（1 个 StartPort + 1 个无参数）


def wechat_exe() -> str:
    """微信可执行路径：优先注册表 InstallPath，回落默认安装路径。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Tencent\Weixin") as k:
            exe = Path(winreg.QueryValueEx(k, "InstallPath")[0]) / "Weixin.exe"
            if exe.exists():
                return str(exe)
    except OSError:
        pass
    return _DEFAULT_EXE


def launch_argv(exe: str, start_port: Optional[int]) -> list[str]:
    """拉起命令行。`start_port=int` → `StartPort=<port>`（**仅第一个实例可用**）；
    `start_port=None`/0 → 无参数（后续实例；带 StartPort= 会自杀）。"""
    return [exe, f"StartPort={start_port}"] if start_port else [exe]


def build_argv(exe: str, port: int) -> list[str]:
    """兼容旧调用：等价 launch_argv(exe, port)。"""
    return launch_argv(exe, port)


def plan_fleet(n_want: int, n_running: int) -> list[tuple[Optional[int], int]]:
    """规划『还需拉起哪些实例』，返回每个的 `(start_port, hook_port)`。

    第一个实例（i==0，当前无实例在跑）用 `StartPort=FIRST_INSTANCE_PORT`（落 30002）；
    后续实例无参数（`start_port=None`，落 30001）。诚实封顶 `MAX_WECHAT_INSTANCES`——
    真机确认再多也只有这两个端口可用，超出的不拉（调用方应据此告警）。
    """
    plan: list[tuple[Optional[int], int]] = []
    for i in range(n_running, min(n_want, MAX_WECHAT_INSTANCES)):
        if i == 0:
            plan.append((FIRST_INSTANCE_PORT, FIRST_INSTANCE_PORT))
        else:
            plan.append((None, DEFAULT_HOOK_PORT))
    return plan


def _default_firewall_runner(argv: list[str]) -> int:
    return subprocess.run(argv, capture_output=True, text=True,
                          timeout=20, creationflags=CREATE_NO_WINDOW).returncode


def add_firewall_block_rule(
    port: int, *,
    runner: Optional[Callable[[list[str]], int]] = None,
    log: Optional[Callable[[str], None]] = None,
) -> bool:
    """挡住该 hook 端口的外部入站访问（hook 自己绑 0.0.0.0）。best-effort，绝不抛。"""
    argv = ["netsh", "advfirewall", "firewall", "add", "rule",
            f"name={FIREWALL_RULE_PREFIX}-{port}",
            "dir=in", "action=block", "protocol=TCP", f"localport={port}"]
    try:
        rc = (runner or _default_firewall_runner)(argv)
    except Exception as e:                              # noqa: BLE001
        if log:
            log(f"[wechat-launcher] 防火墙规则添加失败（端口 {port} 未挡住外部访问）：{e}")
        return False
    if rc != 0 and log:
        log(f"[wechat-launcher] 防火墙规则返回码 {rc}（端口 {port} 可能未挡住外部访问）")
    return rc == 0


def _default_spawn(argv: list[str]) -> int:
    return subprocess.Popen(argv).pid


def _default_clear_crash() -> int:
    from widget.wechat_hygiene import clear_crash_reports
    return clear_crash_reports()


def launch_wechat_instance(
    *,
    start_port: Optional[int],
    hook_port: int,
    exe: Optional[str] = None,
    spawn: Optional[Callable[[list[str]], int]] = None,
    clear_crash: Optional[Callable[[], int]] = None,
    firewall: Optional[Callable[[int], bool]] = None,
    log: Optional[Callable[[str], None]] = None,
) -> Optional[int]:
    """拉起一个微信实例，返回新 pid；失败返回 None（**绝不抛进 GUI**）。

    `start_port`：命令行 `StartPort=` 的值（仅第一个实例可用；后续传 None=无参数）。
    `hook_port`：hook 的**实际落点**端口（无参数实例=30001），用于防火墙规则。
    """
    exe_path = exe or wechat_exe()

    # 先清崩溃残留：否则微信可能弹『上次异常退出，是否修复』（客户绝不该看到）。失败不阻塞。
    try:
        (clear_crash or _default_clear_crash)()
    except Exception as e:                              # noqa: BLE001
        if log:
            log(f"[wechat-launcher] 清崩溃残留失败（忽略）：{e}")

    argv = launch_argv(exe_path, start_port)
    try:
        pid = (spawn or _default_spawn)(argv)
    except Exception as e:                              # noqa: BLE001
        if log:
            log(f"[wechat-launcher] 拉起失败：{e}")
        return None

    try:
        (firewall or (lambda p: add_firewall_block_rule(p, log=log)))(hook_port)
    except Exception as e:                              # noqa: BLE001
        if log:
            log(f"[wechat-launcher] 防火墙规则添加失败（端口 {hook_port} 未挡住外部访问）：{e}")

    if log:
        mode = f"StartPort={start_port}" if start_port else "无参数"
        log(f"[wechat-launcher] 已拉起微信 pid={pid}（{mode}，hook 落点 {hook_port}），请扫码登录")
    return pid or None


def launch_wechat_fleet(
    *,
    n_want: int,
    n_running: int,
    exe: Optional[str] = None,
    spawn: Optional[Callable[[list[str]], int]] = None,
    wait_up: Optional[Callable[[], None]] = None,
    clear_crash: Optional[Callable[[], int]] = None,
    firewall: Optional[Callable[[int], bool]] = None,
    log: Optional[Callable[[str], None]] = None,
) -> list[int]:
    """挂件自管拉起：把在跑的微信实例数补到 `n_want`（封顶 `MAX_WECHAT_INSTANCES`）。

    按 `plan_fleet` 的顺序逐个拉：第一个 `StartPort=30002`、后续无参数→30001。**每拉一个之后
    等它起来**（`wait_up`）再拉下一个——否则第二个可能抢在第一个注册前成为「first」，
    破坏 StartPort 语义。返回成功拉起的新 pid 列表。绝不抛。
    """
    exe_path = exe or wechat_exe()
    if n_want > MAX_WECHAT_INSTANCES and log:
        log(f"[wechat-launcher] 配置了 {n_want} 个微信实例，但一台机最多同跑 "
            f"{MAX_WECHAT_INSTANCES} 个（真机确认），只拉起前 {MAX_WECHAT_INSTANCES} 个")
    pids: list[int] = []
    plan = plan_fleet(n_want, n_running)
    for idx, (start_port, hook_port) in enumerate(plan):
        pid = launch_wechat_instance(
            start_port=start_port, hook_port=hook_port, exe=exe_path,
            spawn=spawn, clear_crash=clear_crash, firewall=firewall, log=log)
        if pid:
            pids.append(pid)
        if idx < len(plan) - 1 and wait_up is not None:
            try:
                wait_up()                               # 等这个起来再拉下一个
            except Exception as e:                      # noqa: BLE001
                if log:
                    log(f"[wechat-launcher] 等待实例就绪失败（忽略，继续）：{e}")
    return pids
