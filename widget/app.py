from __future__ import annotations
import ctypes
import os
from pathlib import Path

from widget.config import WidgetConfig, load_config
from widget.inbound import InboundFilter
from widget.sender import RateLimiter, Sender
from widget.pipeline import Pipeline
from widget.state import RuntimeState
from widget.bridge_pool import BridgePool
from widget.models import InboundMsg, SendResult

def build_pipeline(cfg: WidgetConfig, adapter, bridge, state: RuntimeState) -> Pipeline:
    limiter = RateLimiter(cfg.rate_per_min, cfg.daily_limit)
    sender = Sender(adapter, cfg, limiter)
    self_wxid = adapter.self_wxid()
    state.self_wxid = self_wxid
    state.limiter = limiter
    def on_pending(msg, result) -> None:
        state.add_pending(msg, result)
        action = result.get("record_action", "handoff")
        state.record_inbound(msg, action)
        if action == "handoff_notified" and cfg.handoff_reply:
            state.publish_outbound(msg, cfg.handoff_reply, "ai")

    def on_auto(msg, result) -> None:
        state.record_inbound(msg, "auto_reply")
        reply_text = str(result.get("reply_text") or "")
        if reply_text:
            state.publish_outbound(msg, reply_text, "ai")

    return Pipeline(
        cfg, InboundFilter(), bridge, sender, self_wxid,
        on_pending=on_pending,
        on_auto=on_auto,
        is_ai_enabled=lambda: state.is_ai_enabled(adapter.channel),
    )


def detect_wechat_version() -> str:
    exe_path = Path(os.environ.get(
        "ACS_WECHAT_EXE",
        r"C:\Program Files\Tencent\Weixin\Weixin.exe",
    ))
    if not exe_path.exists():
        return "not found"
    if os.name != "nt":
        return "unknown"
    try:
        version = ctypes.windll.version
        handle = ctypes.c_uint(0)
        size = version.GetFileVersionInfoSizeW(str(exe_path), ctypes.byref(handle))
        if not size:
            return "unknown"
        data = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(str(exe_path), 0, size, data):
            return "unknown"
        value = ctypes.c_void_p()
        value_len = ctypes.c_uint(0)
        if not version.VerQueryValueW(data, "\\", ctypes.byref(value), ctypes.byref(value_len)):
            return "unknown"

        class _FixedFileInfo(ctypes.Structure):
            _fields_ = [
                ("dwSignature", ctypes.c_uint32),
                ("dwStrucVersion", ctypes.c_uint32),
                ("dwFileVersionMS", ctypes.c_uint32),
                ("dwFileVersionLS", ctypes.c_uint32),
                ("dwProductVersionMS", ctypes.c_uint32),
                ("dwProductVersionLS", ctypes.c_uint32),
                ("dwFileFlagsMask", ctypes.c_uint32),
                ("dwFileFlags", ctypes.c_uint32),
                ("dwFileOS", ctypes.c_uint32),
                ("dwFileType", ctypes.c_uint32),
                ("dwFileSubtype", ctypes.c_uint32),
                ("dwFileDateMS", ctypes.c_uint32),
                ("dwFileDateLS", ctypes.c_uint32),
            ]

        info = ctypes.cast(value, ctypes.POINTER(_FixedFileInfo)).contents
        return ".".join(str(part) for part in (
            info.dwFileVersionMS >> 16,
            info.dwFileVersionMS & 0xFFFF,
            info.dwFileVersionLS >> 16,
            info.dwFileVersionLS & 0xFFFF,
        ))
    except Exception:
        return "unknown"


class UnavailablePipeline:
    def release_contact(self, contact_id: str) -> None:
        return

    def handle(self, msg: InboundMsg) -> str:
        return "error"


class UnavailableWeChatAdapter:
    channel = "wechat_personal"

    def __init__(self, reason: str = "WeChat hook unavailable", channel_key: str = "wechat_personal"):
        self.channel = channel_key
        self.reason = reason

    def start(self, on_message) -> None:
        return

    def send_message(self, contact_id: str, text: str, provenance: str = "human") -> SendResult:
        return SendResult(ok=False, error=self.reason)

    def provenance_for(self, text: str) -> str | None:
        return None

    def self_wxid(self) -> str:
        return ""

    def stop(self) -> None:
        return

    def list_sessions(self) -> list[dict]:
        return []

    def read_conversation(self, contact_wxid: str, limit: int = 80) -> list[dict]:
        return []

    def display_names(self, wxids: list[str]) -> dict[str, str]:
        return {wxid: wxid for wxid in wxids}

    def active_session(self) -> str:
        return ""


def choose_pages_adapter(wechat_adapter, wecom_adapters: list) -> tuple[bool, object]:
    """选择会话/群发页默认渠道：个人微信可用时优先个人微信，否则才回退企微。

    旧逻辑只要检测到企微就无条件覆盖个人微信，导致用户启动后看到空的企微会话页；个人微信
    对话只有从待人工入口间接定位后才像是“突然出现”。渠道下拉仍保留，用户可主动切换。
    """
    wechat_ready = wechat_adapter is not None and not isinstance(
        wechat_adapter, UnavailableWeChatAdapter
    )
    if wechat_ready:
        return False, wechat_adapter
    if wecom_adapters:
        return True, wecom_adapters[0]
    return False, wechat_adapter


# ---- M4 微信接线（从 run() 里抽出来，好测；run() 本身是 pragma: no cover 的组装层）----

def wechat_instances(insts) -> list:
    """本轮要接管的微信实例：platform=wechat 且 **enabled**。

    审查修复：`enabled` 此前零读者 —— 用户在 instances.yaml 里把某个号 `enabled: false`，
    挂件照样把它拉起来轮询/发送。
    """
    return [i for i in insts if i.platform == "wechat" and i.enabled]


def is_legacy_wechat(wechat_insts) -> bool:
    """是不是「legacy 单实例」= **没有 instances.yaml，所以配置是我们合成出来的**。

    legacy 必须逐字节沿用旧路径：合成实例的 `self_wxid` 常常是空的（widget_config 里没填），
    走进程驱动认领会因为认不出身份而 park，等于把现网单实例客户的功能整条掐掉。所以 legacy
    分支是**盲补**的：直接按配置端口建 adapter 并往那个端口上的进程写 g_IsLogin。

    ★判据只看 `InstanceConfig.synthesized` 这个由 `widget.instances.legacy_instances()` 打的
    标记，绝不看行上的任何用户可填字段★（审查修复，本轮 Critical）。此前判据是
    「只有一条 && channel_key == 'wechat_personal'」—— 而 docs/DEVLOG.md:24 恰恰处方老客户
    迁移时保留 `channel_key: wechat_personal`（否则后端记忆 tenant+channel+contact 三元组全部
    变孤儿）。于是一份合法的、用户手写的单行 instances.yaml 正好命中判据 → 走 legacy 盲补 →
    客户手动登的未配置号（或端口易主后的陌生进程）挨了这一发跨进程写内存（M4 红线），
    而且此后被记成本号 online：supervisor 不再重试、自愈重补每 10s 再写它一次。
    `load_instances` 显式拒收 YAML 里的 `synthesized` key，用户无法把自己伪装成合成实例。

    ★仍只准在启动时、拿**整份加载出来的配置**调一次★（`run()` 里的 `wechat_legacy`），结果
    显式往下传：重连恒传单元素列表，让下游自己推导过一次，长度判据恒成立。
    """
    return len(wechat_insts) <= 1 and (
        not wechat_insts or bool(getattr(wechat_insts[0], "synthesized", False)))


def prepare_legacy_wechat_selection(wechat_insts, *, legacy: bool, port: int, chooser,
                                    owners_fn=None, choices_fn=None, stop_fn=None,
                                    sleep_fn=None, attempts: int = 20,
                                    pids_fn=None, port_fn=None):
    """同端口双微信时让用户选择一个，并把 legacy 升级到连接级 PID 路由路径。

    账号身份来自该 PID 打开的 xwechat_files 数据目录。后续每条 TCP 连接都会在发送 HTTP 数据前
    用 Windows TCP 表核对服务端 PID，因此不必退出任何微信，也不会把业务请求发给未选账号。
    """
    if not legacy or len(wechat_insts) != 1 or not int(port or 0):
        return legacy, None, ""
    from widget.wechat.selection import (choose_hooked_wechat_process,
                                         choose_shared_port_wechat_process)
    # 生产启动扫描所有带 hook 监听端口的微信；保留 owners_fn 注入路径，让既有共享端口
    # 单元测试和显式诊断仍可只检查一个端口。
    if pids_fn is not None or (owners_fn is None and port_fn is None):
        selected, error = choose_hooked_wechat_process(
            int(port), chooser=chooser, pids_fn=pids_fn, port_fn=port_fn,
            choices_fn=choices_fn,
        )
    else:
        selected, error = choose_shared_port_wechat_process(
            int(port), chooser=chooser, owners_fn=owners_fn, choices_fn=choices_fn,
        )
    if selected is None:
        return legacy, None, error
    # 用户已经明确选人，且进程数据目录给出了权威 wxid；本次运行内存中记录身份，不改配置文件。
    wechat_insts[0].self_wxid = selected.wxid
    return False, selected, ""


def build_routed_wechat_adapter(
    cfg,
    inst,
    selected,
    port: int,
    *,
    client_factory=None,
    adapter_factory=None,
    patch_fn=None,
    owns_port_fn=None,
    patch_attempts: int = 100,
    sleep_fn=None,
    route_port_fn=None,
):
    """为共享端口上的所选 PID 构建连接级路由 adapter；未选微信保持运行。"""
    from widget.adapters.wechat_hook import WeChatHookAdapter
    from widget.hook_patch import ensure_login_patched_routed
    from widget.wechat.pid_routing import build_pid_routed_client, selected_pid_still_listens

    from widget.wechat.hook_rebind import ensure_unique_hook_port

    port = int(port)
    pid = int(selected.pid)
    wxid = str(selected.wxid or "")
    port = int((route_port_fn or ensure_unique_hook_port)(pid, port))
    patcher = patch_fn or ensure_login_patched_routed
    patch_result = ""
    for attempt in range(max(1, int(patch_attempts))):
        patch_result = str(patcher(pid=pid, port=port, wxid=wxid) or "")
        patch_ready = (
            "g_IsLogin 已=1" in patch_result
            or "g_IsLogin 已是 1" in patch_result
            or "patch g_IsLogin 0->1(write=True)" in patch_result
        )
        if patch_ready:
            break
        # 刚启动且仍初始化的微信必须等过 hook 的进程年龄安全闸；身份/端口不匹配等拒绝不能重试掩盖。
        if "暂不补丁" not in patch_result or attempt + 1 >= max(1, int(patch_attempts)):
            raise RuntimeError(f"所选微信安全补丁未就绪：{patch_result or '无状态'}")
        if sleep_fn is None:
            import time
            time.sleep(0.25)
        else:
            sleep_fn(0.25)
    client = (client_factory or build_pid_routed_client)(pid, port)
    state_path = f"{inst.data_dir}/widget_cursor.json" if getattr(inst, "data_dir", "") else None
    adapter = (adapter_factory or WeChatHookAdapter)(
        cfg,
        client=client,
        state_path=state_path,
        self_wxid_override=wxid,
        channel_key=inst.channel_key,
        base_url=f"http://127.0.0.1:{port}",
        verified_pid=pid,
        claimed_wxid=wxid,
        legacy_patch=False,
        patch_login=False,  # 上面已经走共享端口专用的 PID+账号双闸补丁，不能再走唯一端口闸
        owns_port_fn=owns_port_fn or selected_pid_still_listens,
    )
    return adapter, patch_result


def _wxwork_pids() -> list:
    from widget.wecom.winproc import find_process_pids
    return find_process_pids("WXWork.exe")


def _wecom_exe_from_registry() -> str:
    from widget.version_check import wecom_exe
    return wecom_exe()


def _spawn_detached(exe: str) -> None:
    """把客户端当独立进程打开：挂件退出/关控制台都不带走它。

    只给 DETACHED_PROCESS —— 它和 CREATE_NEW_CONSOLE/CREATE_NO_WINDOW **互斥**，混用会让
    CreateProcess 直接 ERROR_INVALID_PARAMETER（等于压根没拉起来）。
    """
    import subprocess
    kw = {"creationflags": 0x00000008} if os.name == "nt" else {}       # DETACHED_PROCESS
    subprocess.Popen([exe], cwd=str(Path(exe).parent or "."), **kw)


def prelaunch_wecom(wecom_insts=(), *, config_path="wecom_hook_config.yaml",
                    pids_fn=None, exe_fn=None, spawn=None, log=None,
                    wait_s: float = 10.0, sleep_fn=None) -> str:
    """打包版冷启动：配了企微、但一个 `WXWork.exe` 都没在跑 → **把企微客户端打开**。

    客户双击挂件时企微通常根本没开 → 注入链一上来就扑空 → 「企微渠道未接入」，客户以为坏了。
    连接器那个 `auto_launch` 默认 False、而且只在 connect 内部才可能生效，指望不上，所以在
    接线之前先开客户端。**只开客户端，扫码登录仍是人工步**（也绝不碰已经在跑的企微：多开
    由客户自己开，我们不插手）。

    exe 来源：`wecom_hook_config.yaml` 的 `wework_exe_path` 优先，其次注册表（version_check）。
    返回真正拉起的 exe 路径；不该拉/找不到/失败 → `""`。**绝不抛**（自动拉起绝不能挡启动）。
    """
    say = log or (lambda _m: None)
    try:
        cfg_path = Path(config_path)
        configured = cfg_path.exists() or bool(wecom_insts)
        if not configured:
            return ""                       # 压根没配企微：不关我们的事
        probe = pids_fn or _wxwork_pids
        if probe():
            return ""                       # 已经有企微在跑：不插手
        exe = ""
        if cfg_path.exists():
            from widget.wecom.config import WeComHookConfig
            exe = (WeComHookConfig.load(cfg_path).wework_exe_path or "").strip()
        if not exe:
            exe = ((exe_fn or _wecom_exe_from_registry)() or "").strip()
        if not exe:
            say("[启动] [!] 没找到企业微信 exe（配置 wework_exe_path 和注册表都没有），"
                "跳过自动打开：请手动打开企业微信后重启挂件")
            return ""
        (spawn or _spawn_detached)(exe)
        say(f"[启动] 已自动打开企业微信（{exe}）：请在弹出的窗口登录，登录后挂件会自动接管")
        # 等进程真的出现再往下走接线，否则第一次注入必然扑空（登录慢的话仍由重连兜底）。
        nap = sleep_fn or _sleep
        for _ in range(int(max(0.0, wait_s) / 0.5)):
            if probe():
                break
            nap(0.5)
        return exe
    except Exception as e:                  # noqa: BLE001 —— 自动打开失败绝不挡挂件启动
        say(f"[启动] [!] 企业微信自动打开失败（忽略，可手动打开企业微信）：{e}")
        return ""


def _sleep(seconds: float) -> None:
    import time
    time.sleep(seconds)


def _wechat_pids_default() -> list:
    from widget.wechat.ports import wechat_pids
    return wechat_pids()


def _wechat_port_for_pid(pid: int):
    from widget.wechat.ports import listening_port_for_pid
    return listening_port_for_pid(pid)


def plan_wechat_autolaunch(*, legacy: bool, pids_fn=None, port_for_pid=None,
                           settle_s: float = 3.0, sleep_fn=None, log=None) -> str:
    """自管拉起微信要不要拉、按哪条路拉：`""`=不拉 / `"legacy"`=无参单号 / `"fleet"`=按队拉。

    ★判据是「机器上一个 `Weixin.exe` 都没有」，不是「有几个已经在听 hook 端口」★（审查修复）。
    端口计数为 0 的现场恰恰是这版要伺候的两类客户：
      (a) 微信自动更新把 `version.dll` 覆盖了 → hook 没了 → 端口恒探不到。按端口判就会在客户
          **已登录的会话**旁边每次起挂件都再弹一个（同样没 hook 的）微信 —— 纯噪音，且永远
          不会自愈；正确做法是让版本页去提示「需降级」。
      (b) 开机自启 / 客户先开微信紧接着起挂件 → 那一瞬端口还没绑上。只采一次样就判 = 多拉一个
          全新实例，还回头跟老的抢 30001。

    所以：**有微信进程就一律不拉**；并且在有进程、端口还没出现时复探到 `settle_s`（把「正在起」
    和「真的没 hook」分开，也顺手让后面的 discovery 认领少扑空一次）。探测本身炸了 → 返回
    `""`（fail closed：不知道有没有微信，就绝不乱拉）。**绝不抛**。

    注：legacy 分支对「只用企微的客户」也会成立（`legacy_instances()` 无条件合成一条 wechat
    实例）；这类机器上通常压根没装微信 → `launch_wechat_instance` 找不到 exe 抛错 → 调用方
    已经 try/except 成一行日志，不挡启动。
    """
    say = log or (lambda _m: None)
    try:
        probe = pids_fn or _wechat_pids_default
        port_of = port_for_pid or _wechat_port_for_pid
        pids = list(probe() or [])
        if not pids:
            return "legacy" if legacy else "fleet"
        nap = sleep_fn or _sleep
        for _ in range(max(1, int(max(0.0, settle_s) / 0.5))):
            if any(port_of(p) for p in pids):
                break
            nap(0.5)
        say(f"[启动] 已有微信在跑（pid={','.join(str(p) for p in pids)}），不自动拉起；"
            f"若显示未连接，多半是 hook 被微信自动更新覆盖了 —— 去『版本』页强制降级")
        return ""
    except Exception as e:                  # noqa: BLE001 —— 决策失败 = 不拉（绝不挡启动）
        try:
            say(f"[启动] [!] 微信进程探测失败（本轮不自动拉起）：{e}")
        except Exception:                   # noqa: BLE001
            pass
        return ""


def make_downgrade_fn(fn, name: str, log=None):
    """把 `widget.downgrade.*` 的 True/False 语义接成版本页要的「抛异常=失败」语义。

    downgrade_* 绝不抛、失败只返回 False；版本页只认异常。不转换的话，取消 UAC/缺安装包
    也会显示「降级成功」（假绿），客户照着重启还是老版本。返回的是零必填参数的可调用，
    版本页据此判定不喂 progress 回调。
    """
    def _run() -> None:
        if not fn(log=log or print):
            raise RuntimeError(f"{name}降级未完成（可能被取消 / 缺自带安装包 / 需管理员），详见日志")
    return _run


def _default_wechat_manager(wechat_insts, cfg, bridges, state, hub, log=print, **kw):
    from widget.wechat.manager import WeChatManager
    return WeChatManager(wechat_insts, cfg, bridges, state, hub, logger=log, **kw)


def start_wechat_channels(wechat_insts, cfg, bridge, state, hub, *,
                          legacy=None, all_insts=None, reserved_ports=None,
                          injected_adapter=None, manager_factory=None, log=print,
                          pool=None):
    """把 `wechat_insts` 这几个微信实例接进 hub。返回 `(adapter, pipe, pids, ports, parked)`。

    多实例走 **WeChatManager 进程驱动认领**：端口→账号的绑定由「枚举进程 → 问端口 →
    问身份 → OS 佐证 → 匹配 instances.yaml」决定，配置里的 `hook_port` 只是提示。审查修复前
    这里是无条件信配置直建 adapter —— 配置一旦与真实监听者错位（StartPort 抢占/启动顺序/
    端口复用），甲号的 adapter 会去轮询并从乙号的微信发消息。

    参数（审查修复，整分支审查 Critical）：
    - `legacy`：是不是 legacy 单实例路径。**由启动时的整份配置判定一次后显式传进来**；
      留空才回落到按本次传入的列表推导（只有启动那一次调用可以留空，见 `is_legacy_wechat`）。
      重连恒传单元素列表，靠推导 = 恒判 legacy = 绕过整条认领链直接写内存。
    - `all_insts`：**匹配全集**。重连时 `wechat_insts` 只有本号一条，但认领必须拿全部已配置
      账号去比对 wxid，否则乱报的 /GetSelfProfile 会把乙号的进程硬套成甲号。
    - `reserved_ports`：`{port: channel_key}`，已被别的实例占着的端口，本轮不许重复认领。
    """
    from widget.instance_runtime import build_instance
    adapter = None
    pipe = None
    pids: dict = {}
    ports: dict = {}
    parked: dict = {}
    is_legacy = is_legacy_wechat(wechat_insts) if legacy is None else bool(legacy)

    # ★M5★ 每个实例用**自己租户的 Bridge**（隔离靠 token）。`pool` 传进来才走 N-Bridge；
    # 没传（既有测试/旧调用）回落到单个 `bridge`，行为逐字节不变。
    def _bridge_for(inst):
        return pool.for_instance(inst) if pool is not None else bridge

    if injected_adapter is not None or is_legacy:
        for inst in wechat_insts:
            try:
                if injected_adapter is None:
                    from widget.instances import effective_hook_port
                    from widget.wechat.ports import listening_pids_for_port
                    port = int(effective_hook_port(inst) or 0)
                    owners = listening_pids_for_port(port) if port else []
                    if port and not owners:
                        raise RuntimeError(
                            f"无法确认个人微信端口 {port} 的唯一监听进程。为防止读取到错误账号，"
                            "本次已停止接管；请确认微信已登录后重新点击“启动AI客服.bat”。"
                        )
                    if len(owners) > 1:
                        raise RuntimeError(
                            f"个人微信端口 {port} 被 {len(owners)} 个微信实例共同占用"
                            f"（pid={','.join(map(str, owners))}）。为防止跨账号读取会话，"
                            "本次已停止接管；请只保留一个微信窗口后重新点击“启动AI客服.bat”，"
                            "多账号请在 instances.yaml 为每个账号配置独立 StartPort。"
                        )
                    a, p = build_instance(inst, cfg, _bridge_for(inst), state, legacy=True)
                else:  # 测试/预注入单实例路径
                    a, p = build_instance(inst, cfg, _bridge_for(inst), state,
                                          adapter=injected_adapter)
                    verified_pid = int(getattr(injected_adapter, "_verified_pid", 0) or 0)
                    routed_port = int(getattr(injected_adapter, "hook_port", lambda: 0)() or 0)
                    if verified_pid and routed_port:
                        pids[inst.channel_key] = verified_pid
                        ports[inst.channel_key] = routed_port
                hub.register(inst.channel_key, a, p)
                state.hook_ok = True
                state.hook_error = ""
                log(f"[启动] hook 身份认领完成 channel={inst.channel_key} "
                    f"self_wxid={state.get_self_wxid(inst.channel_key)} | 启动轮询时将建立新消息基线")
            except Exception as e:
                state.hook_ok = False      # hook 未就绪：不启动轮询，只显示降级面板
                state.hook_error = str(e)
                log(f"[启动] [X] hook 未就绪({e})：微信没开/未注入/未 patch g_IsLogin，不启动轮询")
                log(f"[startup] hook unavailable ({e}); showing full workbench without WeChat polling")
                a = UnavailableWeChatAdapter(str(e), channel_key=inst.channel_key)
                p = UnavailablePipeline()
                hub.register(inst.channel_key, a, p)
            if adapter is None:    # 下游沿用「首个/legacy」微信 adapter/pipe
                adapter, pipe = a, p
        return adapter, pipe, pids, ports, parked

    # 匹配全集 vs 本轮目标：全集决定「这个 wxid 是谁」，目标决定「这轮允许接管谁」。
    universe = list(all_insts) if all_insts else list(wechat_insts)
    targets = {i.channel_key for i in wechat_insts}
    bridges = {i.channel_key: _bridge_for(i) for i in universe}   # ★M5★ 每账号自己租户的 Bridge（隔离）
    mgr = (manager_factory or _default_wechat_manager)(
        universe, cfg, bridges, state, hub, log=log,
        targets=targets, reserved_ports=reserved_ports)
    mgr.start()
    pids, ports, parked = dict(mgr.pids), dict(mgr.ports), dict(mgr.parked)
    for ck, ad in mgr.adapters.items():
        log(f"[启动] hook 身份认领完成 channel={ck} self_wxid={state.get_self_wxid(ck)} "
            f"端口={ports.get(ck)} | 启动轮询时将建立新消息基线")
        if adapter is None:
            adapter, pipe = ad, hub.pipeline(ck)
    for inst in mgr.unclaimed:
        # 配了但这轮没发现对应进程（微信没开/未注入/未登录）：注册降级 adapter，工作台照常显示。
        log(f"[启动] [X] {inst.channel_key} 未发现对应微信进程：不启动该号轮询")
        hub.register(inst.channel_key,
                     UnavailableWeChatAdapter("微信未开或未注入", channel_key=inst.channel_key),
                     UnavailablePipeline())
    if parked:
        log(f"[启动] [!] 未认领的微信进程已 park（不接管）：{parked}")
    state.hook_ok = adapter is not None
    state.hook_error = "" if state.hook_ok else "未发现可接管的微信实例"
    return adapter, pipe, pids, ports, parked


def load_instances_or_degrade(cfg=None, *, load=None, legacy=None, log=print) -> list:
    """加载 instances.yaml；**任何失败都降级，绝不让异常穿出 `run()`**。

    审查修复：`run()` 此前裸调 `load_instances()`。挂件在客户机上跑在 pythonw 下（无控制台），
    异常穿出去的表现是「双击了没反应」—— 客户看不到任何错误、也不会想到去看日志，客服直接
    停摆。一份用户手写的 instances.yaml（少写一个字段、缩进错、YAML 语法错）就够触发。

    降级阶梯：
      ① instances.yaml 正常 → 照常返回；
      ② 坏了 → 退回 legacy 合成的单实例（老客户本来就跑这条路），打印真实原因；
      ③ 连 legacy 也失败（企微配置也坏）→ 只合成个人微信一条；
      ④ 全崩 → 返回空列表：不接管任何渠道，但工作台照样起来，客户至少能看见界面和报错。
    """
    from widget import instances as _instances
    load = load or _instances.load_instances
    legacy = legacy or _instances.legacy_instances
    try:
        return load()
    except Exception as e:                              # noqa: BLE001
        log(f"[启动] [!] instances.yaml 加载失败（{e}）：本次忽略多实例配置，按单实例启动")
    try:
        return legacy(wc=cfg)
    except Exception as e:                              # noqa: BLE001
        log(f"[启动] [!] 单实例回退也失败（{e}）：再试一次「只要个人微信」")
    try:
        return legacy(wc=cfg, wecom_cfg_path="")
    except Exception as e:                              # noqa: BLE001
        log(f"[启动] [X] 配置全部不可用（{e}）：本次不接管任何渠道，工作台仍会启动")
        return []


def _default_pid_for_port(port: int):
    from widget import hook_patch
    return hook_patch._hook_pid(port)


def _default_port_for_pid(pid: int):
    """这个 pid 此刻在听哪个 hook 端口（区间内）。**唯一的实现**在 widget/wechat/ports。"""
    from widget.wechat.ports import listening_port_for_pid
    return listening_port_for_pid(int(pid))


def pid_still_owns_port(pid: int, port: int, *, port_for_pid=None) -> bool:
    """那台微信是不是**还在、且还占着同一个端口**。查不出来一律返回 False（fail closed）。

    ★为什么这是安全判据而不是优化★：adapter 手里只有端口，端口易主它不知道。微信重启会换
    pid，而端口是从 30001 起分配的 —— 甲的微信一退，同一个 30001 立刻可能属于**另一个已配置
    账号**的微信（客户按文档跑 `pythonw start_wechat.py`，不带 --port 就是绑 30001）。此时旧
    adapter 若继续轮询：游标字典的 key 是会话表 md5，换了台微信全不认识 → cursor=0 → 把新主人
    每个会话的**全部历史**当新消息喷进旧号的 pipeline（错租户、错客户记忆、错自动回复），
    出站的人工回复/群发也从别人的微信发出去。

    实现只有**一份**，在 `widget/wechat/ports.py` —— `WeChatHookAdapter` 每次收发前的归属闸
    调的也是它（本轮 H2）。这里只是把 app 侧的 OS 探针接上去。
    """
    from widget.wechat.ports import pid_still_owns_port as _owns
    return _owns(pid, port, port_for_pid=port_for_pid or _default_port_for_pid)


def wechat_identity_drifted(hub, channel_key: str) -> bool:
    """hub 里这条渠道此刻的 adapter 是不是已经判定「登的不是认领时那个号」了。

    ★H5★ 纯读 adapter 已有的结论（`WeChatHookAdapter.identity_drifted`），**不探 OS** ——
    给 `park_offline_channel` 这类「顺手问一句」的地方用。真正去付那 130~280ms 的是
    `wechat_identity_healthy`，且只在 supervisor 自己的后台线程上。
    降级壳子 / 企微 adapter 没有这个方法 → 一律当作「没漂移」（它们本来就不轮询个人微信）。
    """
    fn = getattr(hub.adapter(channel_key), "identity_drifted", None)
    try:
        return bool(fn()) if callable(fn) else False
    except Exception:                       # noqa: BLE001 —— 读个缓存都失败：别打下活渠道
        return False


def wechat_identity_suspect(hub, channel_key: str) -> bool:
    """这条渠道此刻是不是「便宜信号说好像换号了、还没被昂贵确认裁决」。纯读，不探 OS。

    存疑期间收发全拒（fail closed），所以它有资格**插队**做一次昂贵确认 —— 那是事件驱动的
    一次性开销，不是「按时间表」的周期开销（规则 3 约束的是后者）。
    """
    fn = getattr(hub.adapter(channel_key), "identity_suspect", None)
    try:
        return bool(fn()) if callable(fn) else False
    except Exception:                       # noqa: BLE001
        return False


def wechat_identity_healthy(hub, channel_key: str) -> bool:
    """★规则 3★ 周期性重新确认身份 —— 这是**全项目唯一**按时间表付 `open_files` 那 130~280ms
    的地方，而且只跑在 InstanceSupervisor 自己那条 5s 的后台线程上（本函数即它的 health_fn）。

    为什么必须有它：读路径（轮询稳态 / 会话页 / 待人工页 / 名单 / 头像）已经不再自己探 OS 了
    （R2：那会把 Qt GUI 线程冻住 130~280ms）。它们只看结论。于是「结论会不会过期」这件事
    必须有人负责 —— 就是这里。没有它，一台原地换了号、又恰好没人点开界面的微信，可以永远
    挂着 online，认领链再也不重跑（H5 的第二半）。

    `refresh_identity` 自己吃 `identity_ttl`（5.0s ≈ 一个 tick）：一个 tick 最多探一次。
    """
    ad = hub.adapter(channel_key)
    fn = getattr(ad, "refresh_identity", None)
    if callable(fn):
        try:
            return bool(fn())
        except Exception:                   # noqa: BLE001 —— 探测失败绝不打下活渠道
            return True
    return not wechat_identity_drifted(hub, channel_key)


def park_offline_channel(inst_state, hub, *, port_for_pid=None, log=None) -> bool:
    """某个号掉线了：如果它原来那台微信已经不在（或端口已易主），**立刻停掉它的 adapter**
    并换上降级壳子，行留在 offline 让 supervisor 继续退避重试。返回是否真的停了。

    审查修复（本轮 Critical，S2 的另一半）：supervisor 把实例标成 offline 只是改了个状态字段，
    adapter 的轮询线程还在自己跑。掉线的常见原因就是那台微信退出了 —— 它的端口随时可能被
    **另一个已配置账号**的微信接手（重连认领是正确的，但那救不了这条谁也没叫停的旧线程）：
    于是乙的每条客户消息同时进乙的渠道（对）和甲的渠道（错租户 + 客户收双份回复）。

    反向属性同样重要：**进程还在、端口还是它的**（只是这一轮 psutil 采不到内存：权限抖动/
    句柄瞬时失败）时一个字节都不动 —— 停掉一条活着的渠道会让客户静默失去自动回复。

    ★H5★ 例外：adapter 自己已经判定「这台微信登的不是认领时那个号了」（原地退出登录扫了别的
    码 / pid 复用）。这种情况下 pid 与端口全都对得上，上面那条反向属性会把它当成活渠道留下 ——
    但它指着的是**别人的账号**。所以身份漂移一律停掉换壳子，让 supervisor 重跑认领链。
    """
    ck = getattr(inst_state, "channel_key", "")
    if not ck or getattr(inst_state, "platform", "") != "wechat":
        return False
    old = hub.adapter(ck)
    if old is None or isinstance(old, UnavailableWeChatAdapter):
        return False                       # 已经是壳子（或没注册过）：没有线程在轮询
    if not wechat_identity_drifted(hub, ck) and pid_still_owns_port(
            getattr(inst_state, "pid", 0), getattr(inst_state, "port", 0),
            port_for_pid=port_for_pid):
        return False                       # 那台微信还在、端口没易主、号也没换 → 别动活渠道
    try:
        old.stop()
    except Exception:                      # noqa: BLE001 —— 停不掉也要把注册换成壳子
        pass
    reason = "微信已退出或已换号：已停止轮询，等待重新认领"
    hub.register(ck, UnavailableWeChatAdapter(reason, channel_key=ck), UnavailablePipeline())
    inst_state.pid = 0                     # 陈旧 pid 不留：别让任何按 pid 动作的逻辑用到它
    if log:
        log(f"[自愈] {ck} 掉线且端口 {getattr(inst_state, 'port', 0)} 已非原进程所有："
            f"停止轮询，等 supervisor 重新认领")
    return True


def wechat_instance_states(wechat_insts, pids: dict, ports: dict, *, pid_for_port=None,
                           legacy=None) -> list:
    """逐实例算出实例控制台的状态行 —— pid/端口**每个实例各问各的**。

    审查修复①：老写法在循环体里跑 `hook_patch._hook_pid()`（无参 = 只看 30001），把那一个
    监听者的 pid 盖给了全部 N 行 —— 控制台 N 行同一个 pid，supervisor 反复采同一个进程，
    另外 N-1 个真微信进程无人监控，任何按 `inst.pid` 动作的逻辑都会作用到错的进程上。

    审查修复②（整分支审查 Critical）：**没被 manager 认领的号绝不按「配置端口」现场探 pid**。
    多开路径下端口→账号的唯一权威是 WeChatManager 的认领结果（`pids`）：配置里写着 30001 的
    甲号，如果这一轮甲的微信没开、而客户手动登了个没配置的号 C 抢到了 30001，manager 会正确地
    park 掉 C，app.py 却会把 C 的 pid 冒领成甲这一行 → ㈠ `repatch_wechat` 每 10s 往一个
    manager 明确拒绝接管的微信进程里跨进程写 g_IsLogin（M4 存在的意义就是杜绝这种写）；
    ㈡ 状态被写成 online，`InstanceSupervisor` 只对 offline/parked 重试认领，甲号在本次进程
    生命周期内彻底废掉，控制台还把陌生进程的内存显示成甲号的。

    legacy 单实例（无 instances.yaml 合成的 wechat_personal）不走 manager，`pids` 恒空，
    仍按自己的 base_url 端口探一次 —— 那个端口上就是它自己那台微信，行为逐字节不变（规则 4）。

    `legacy` 同样**由启动时的整份配置判定后显式传进来**（留空才回落到推导）：重连只传本号
    一条实例，靠列表推导会把「channel_key 恰好叫 wechat_personal 的已配置号」当成 legacy，
    于是又去按配置端口探 pid、把端口上的陌生进程冒领成本号并标成 online。
    """
    from widget.instance_supervisor import InstanceState
    from widget.instances import effective_hook_port
    probe = pid_for_port or _default_pid_for_port
    legacy = is_legacy_wechat(wechat_insts) if legacy is None else bool(legacy)
    states = []
    for inst in wechat_insts:
        ck = inst.channel_key
        port = int(ports.get(ck) or effective_hook_port(inst) or 0)
        pid = int(pids.get(ck) or 0)
        if not pid and port and legacy:
            try:
                pid = int(probe(port) or 0)
            except Exception:                       # noqa: BLE001 —— 探不到就当未接管
                pid = 0
        states.append(InstanceState(
            channel_key=ck, platform="wechat", account_id=inst.account_id,
            display_name=inst.display_name, pid=pid,
            status=("online" if pid else "parked"), mem_mb=0, ai_enabled=True, port=port))
    return states


def repatch_wechat(states, *, patch_fn=None, log=None) -> list:
    """自愈重补：对**每个**微信实例按它自己的端口确保 g_IsLogin=1（消费 `InstanceState.port`）。

    审查修复：老写法是无参 `ensure_login_patched()` —— 只补 30001 那一个号；多开时另外
    N-1 个号在微信重启后补不上，QueryDB 恒空（表现为「收不到消息」）。
    按端口（而不是记下来的 pid）打：pid 由 hook_patch 现场重扫，微信重启换了 pid 也能自愈。

    审查修复（整分支审查 Critical，第二道闸）：**只对 online 的号打**。parked/offline 的行
    带的是「配置端口」，那个端口上的监听者可能是一台我们明确拒绝接管的微信（未认领的号/
    身份存疑/重复认领）—— 往它进程里写内存正是 M4 要杜绝的那一类写。等它被 supervisor
    重新认领成 online，自愈重补自然恢复。

    审查修复（第三道闸）：连**已验证 pid** 一起传给 hook_patch。只给端口的话 hook_patch 会
    认为 pid 不是显式的、跳过 pid/端口配对闸，于是每 10s 一次地写「此刻谁在听这个端口」——
    微信重启后端口易主、或别的程序占了这个端口，就会写进陌生进程。带上 pid 后配对不成立
    直接拒绝本轮（下轮 supervisor 重新认领后自然恢复）。
    """
    from widget import hook_patch
    fn = patch_fn or (
        lambda port, pid: hook_patch.ensure_login_patched(port=port, pid=pid))
    out: list = []
    done: set = set()
    for st in states:
        if getattr(st, "platform", "") != "wechat":
            continue
        if getattr(st, "status", "") != "online":
            continue
        port = int(getattr(st, "port", 0) or hook_patch.HOOK_PORT)
        if port in done:
            continue
        done.add(port)
        try:
            out.append(fn(port, int(getattr(st, "pid", 0) or 0)))
        except Exception as e:                      # noqa: BLE001 —— 一个号失败不拖垮其它
            if log:
                log(f"[自愈] {getattr(st, 'channel_key', '')} 端口 {port} 重补失败（忽略）：{e}")
    return out


def reconnect_wechat_instance(inst_state, target, cfg, bridge, state, hub, *,
                              all_insts=None, reserved_ports=None, legacy=False,
                              on_message=None, on_ready=None, log=print,
                              start_fn=None, port_for_pid=None, pool=None) -> bool:
    """重跑一次本号的认领。跑在 supervisor 后台线程，**绝不碰 Qt widget**。返回是否重连成功。

    ★安全铁律（整分支审查 Critical，本函数是三条攻击的交汇点）★
    重连**必须走完整条 WeChatManager 认领链**（枚举进程 → 问端口 → 问身份 → OS 佐证 →
    对**全部**已配置账号匹配），认领成功才允许往那个进程写 g_IsLogin：
    - `legacy` 由启动时判定后显式传入（默认 False = 走认领链）。旧代码是在下游按「手里这个
      列表有几条」现推的，而重连恒传单元素列表 → 长度判据恒成立 → 已配置的老号（DEVLOG:24
      让迁移客户保留 `channel_key: wechat_personal`）恒判 legacy → 直接按配置端口建 adapter：
      端口上如果是客户手动登的未配置号（manager 启动时已 park 它），就写进了陌生微信，
      而且被记成本号 online（此后 supervisor 不再重试、自愈重补每 10s 再写它一次）。
    - `all_insts` 传**全部**已配置微信实例作匹配全集。只拿本号一条去匹配的话，端口乱报出
      任何 wxid 都会被硬套到本号上 —— 乙号在线的微信会被认领成甲号的渠道。
    - `reserved_ports` 传别的实例正在用的端口，本轮一律不碰。

    审查修复（整分支审查）：认领失败必须把 hub 里原来的注册**原样放回**。
    `start_wechat_channels` 在认领失败时会无条件塞一个 `UnavailableWeChatAdapter` +
    `UnavailablePipeline` 进 hub，而旧的真 adapter 还在自己的线程里轮询 —— 不还原的话：
    入站消息落进 `UnavailablePipeline.handle` 恒返回 "error"（静默丢消息），
    `hub.adapter(ck)`（转人工控制器/人工回复都走它）变成 `send_message` 恒 ok=False 的空壳。
    一次瞬时失败（psutil 取不到内存 → offline → 重连 → GetSelfProfile 这轮没认出来）
    就足以让一条活着的渠道永久变砖，且下一轮采样成功后状态回 online、再也不会重试。

    审查修复（本轮 Critical）：上面那条「原样放回」**只在原进程还在、且还占着原端口时成立**。
    掉线的常见原因恰恰是那台微信退出了，而它的端口随时会被另一台微信接手（客户按文档跑
    `pythonw start_wechat.py`，不带 --port 就是绑缺省 30001，扫的可能是另一个已配置账号）。
    旧 adapter 只认端口：放回去 = 它继续轮询别人的微信，游标 key 全不认识 → cursor=0 →
    新主人每个会话的**全部历史**作为本号入站进本号租户，出站也从别人的微信发出。
    所以放回前先问操作系统 `pid_still_owns_port`；不成立就停掉旧 adapter、留降级壳子，
    行保持 offline/parked 让 supervisor 继续重试。

    Qt 页面的重绑由 `on_ready(channel_key)` 抛回 GUI 线程做（见 `rebind_pages_after_reconnect`）。
    """
    ck = inst_state.channel_key
    old_ad = hub.adapter(ck)
    old_pipe = hub.pipeline(ck)
    # 认领前先记下「本号原来绑的是哪个 pid/端口」——认领链会就地改写 inst_state。
    prev_pid, prev_port = int(getattr(inst_state, "pid", 0) or 0), \
        int(getattr(inst_state, "port", 0) or 0)
    prev_ok, prev_err = state.hook_ok, state.hook_error
    start = start_fn or start_wechat_channels
    # ★M5★ 重连也拿**对租户的** Bridge：把 pool 传下去，start 内部按每账号 pool.for_instance。
    _ad, _pipe, pids, ports, _parked = start(
        [target], cfg, bridge, state, hub, log=log,
        legacy=legacy, all_insts=all_insts, reserved_ports=reserved_ports, pool=pool)
    new = hub.adapter(ck)
    if new is None or isinstance(new, UnavailableWeChatAdapter):
        # 这轮还是没认领到：整体健康还原（一个号没回来不该把别的号拉成红灯），
        # 保持 offline/parked 等下次退避重试。
        if old_ad is not None and old_ad is not new:
            if isinstance(old_ad, UnavailableWeChatAdapter) or pid_still_owns_port(
                    prev_pid, prev_port, port_for_pid=port_for_pid):
                # 原进程还在、端口还是它的（或原本就是个不轮询的降级壳子）→ 原样放回，
                # 一次瞬时失败绝不打碎活渠道。
                hub.register(ck, old_ad, old_pipe)
            else:
                # 端口易主/进程没了：旧 adapter 只认端口，放回去就是在读写别人的微信。
                try:
                    old_ad.stop()
                except Exception:                   # noqa: BLE001 —— 停不掉也要留壳子
                    pass
                inst_state.pid = 0                  # 陈旧 pid 不留
                if getattr(inst_state, "status", "") == "online":
                    # 原进程没了却还挂着 online：supervisor 只对 offline/parked 重试，
                    # 留着 online 等于这个号在本次进程生命周期内彻底废掉。
                    inst_state.status = "offline"
                if log:
                    log(f"[自愈] {ck} 端口 {prev_port} 已非原进程（pid={prev_pid}）所有："
                        f"停掉旧轮询，等下轮重新认领")
        state.hook_ok, state.hook_error = prev_ok, prev_err
        return False
    if old_ad is not None and old_ad is not new:
        try:
            old_ad.stop()               # 换 adapter 前停旧轮询线程：两个线程收同一个号会重复回复
        except Exception:               # noqa: BLE001
            pass
    st = wechat_instance_states([target], pids, ports, legacy=legacy)[0]
    inst_state.pid, inst_state.port = st.pid, st.port
    inst_state.status = "online"
    inst_state.last_error = ""
    state.hook_ok, state.hook_error = True, ""
    if on_message is not None:
        new.start(on_message=on_message)   # 抛出交给 supervisor：它会记 last_error 并退避重试
    if log:
        log(f"[自愈] {ck} 已重新接管 pid={st.pid} 端口={st.port}")
    if on_ready is not None:
        on_ready(ck)
    return True


def rebind_pages_after_reconnect(channel_key, adapter, *, pages_channel_key,
                                 handoff_page=None, broadcast_page=None,
                                 history_page=None, contact_source=None,
                                 avatar_provider=None, on_refresh=None) -> bool:
    """重连成功后把各页面重绑到新 adapter。**必须跑在 GUI 线程**（由 Qt 信号排队过来）。

    审查修复（legacy 回归）：M3 里 `_attach_hook` 做的就是这件事，M4 的重连回调为了线程安全
    不碰 Qt，结果这块活没人接。本项目文档记录的常见客户顺序就是「先起挂件、后开微信」——
    那时群发页/待人工页拿到的是降级 stub；重连成功却不重绑，这两页会永远绑着 stub
    （`send_message` 恒 ok=False，报的还是过期的理由），客户看不到任何错误。

    ★H3★ 联系人源和头像也必须在这里一起重绑：只把 `broadcast_page.adapter` 换掉，
    名单/头像就还钉在旧端口上 —— 那个端口此刻多半已经属于**另一台微信**了
    （见 `widget/guarded_read.py`）。而且顺序上必须**先重绑、后 refresh_friends**。
    """
    if adapter is None or not channel_key or channel_key != pages_channel_key:
        return False
    if handoff_page is not None:
        handoff_page.adapter = adapter
    if broadcast_page is not None:
        broadcast_page.adapter = adapter
    for side_panel in (contact_source, avatar_provider):
        rebind = getattr(side_panel, "set_adapter", None)
        if callable(rebind):
            try:
                rebind(adapter)
            except Exception:           # noqa: BLE001 —— 副表盘重绑失败不该回滚主路径的重绑
                pass
    todo = []
    if history_page is not None:
        todo.append(lambda: history_page.channel_reconnected(channel_key))
    if broadcast_page is not None and getattr(broadcast_page, "refresh_friends", None):
        todo.append(broadcast_page.refresh_friends)
    if on_refresh is not None:
        todo.append(on_refresh)
    for fn in todo:
        try:
            fn()
        except Exception:               # noqa: BLE001 —— 某页刷新失败不该回滚已完成的重绑
            pass
    return True


def primary_wechat_base_url(adapter, ports: dict, cfg) -> str:
    """副表盘（群发联系人源/头像）钉的「主实例」hook 地址 —— 必须与 `adapter` 是同一个号。

    审查修复：群发的收件人名单读自 contact.db（走这个 base_url），发送走页面持有的 adapter。
    两者错位 = 拿甲号的好友表从乙号发出去 —— 不是乙好友的发不出，两边都是好友的收到来自
    错号的群发。群发是真在花钱的，不能只在标题上声称是哪个号在发。
    """
    ck = getattr(adapter, "channel", "") or ""
    port = int((ports or {}).get(ck) or 0)
    return f"http://127.0.0.1:{port}" if port else cfg.hook_base_url


def side_panel_bridge(pool, inst_by_ck: dict, pages_ck: str, cfg):
    """副表盘（群发草稿 / 工作台 画像·标签·ai-mute）持有的「主实例」Bridge。

    ★M5/D5 整分支审查修复★：这些副表盘是**进程级单面板**，它们真正操作的联系人是
    `pages_ck` 那条渠道（企微在场即企微，见 `pages_adapter`）。所以它们持有的 Bridge/token
    必须是 **`pages_ck` 那条渠道的租户**——绝不能钉到个人微信 `adapter.channel` 那条。

    旧写法从 `adapter.channel`（个人微信那条）派生：微信租户A + 企微租户B 同跑时，面板操作 B
    的客户却握着 A 的 token → `HistoryPage.summarize_to_kb` 把 B 号客户历史蒸馏进 **A 租户
    KB**（正是 D5 syncer 修复要杜绝的污染，当时只堵了自动 syncer，这个手动「入库」按钮和
    ai-mute 仍漏）；`set_ai_mute` 把静音写在 A，而 B 的管线在 B 自己的 Bridge 上读——静音
    静默失效且污染错租户。纯多开企微（无个人微信）时 `adapter.channel` 为空，旧写法回落到
    base/占位租户，同样不是被操作的那个企微号。

    取不到 `pages_ck` 对应实例（该渠道没接上）→ 回落基础/默认 Bridge（`for_config`），不崩。
    """
    inst = inst_by_ck.get(pages_ck)
    return pool.for_instance(inst) if inst is not None else pool.for_config(cfg)


def build_wechat_side_panels(hub, primary_channel_key: str, cfg, *,
                             contact_source_factory=None, avatar_factory=None):
    """副表盘（群发联系人源 / 头像）的接线。返回 `(live_adapter_fn, contact_source, avatars)`。

    ★为什么这段必须住在 run() 外面（本轮 H6）★：`run()` 是 `pragma: no cover` 的组装层，
    它的接线此前只被 `inspect.getsource(app.run)` 的**子串 grep**「守着」。审查者证明了那种守
    一文不值 —— 把 `HookContactSource(cfg, adapter_fn=…)` 换成钉死端口的写法、同时把被 grep
    的字面量留在一段死代码里，整套 widget 测试**全绿**；把 `lambda: hub.adapter(ck)` 换成
    `lambda: adapter`（一行、无死代码）打掉重连重绑，547 条测试**也全绿**。
    所以两条安全属性下沉到这里，由 `tests/widget/test_m4_run_wiring.py` 用行为测试锁住：
      ① 名单/头像**不持有任何 hook 地址**，一律走 `adapter.query_db`（与收发同一道归属+身份闸）；
      ② 取的是 **hub 里此刻注册的那个 adapter**（重连换了 adapter 就跟着换），
         而不是启动时捕获的那个对象。
    """
    from widget.broadcast.contacts import HookContactSource
    from widget.ui.avatars import AvatarProvider

    def live_adapter():
        # 每次读之前现取：端口会易主、账号会原地换、重连会换 adapter —— 捕获任何一个都是错的。
        return hub.adapter(primary_channel_key)

    contact_source = (contact_source_factory or HookContactSource)(cfg, adapter_fn=live_adapter)
    avatars = (avatar_factory or AvatarProvider)(adapter_fn=live_adapter)
    return live_adapter, contact_source, avatars


def build_instance_supervisor(hub, cfg, bridge, state, *, insts, wechat_insts, legacy,
                              on_message, on_ready, ready_gate=None, injected_adapter=None,
                              sampler=None, log=print,
                              supervisor_factory=None, reconnect_fn=None,
                              clock=None, confirm_spacing_s: float = 5.0, pool=None):
    """实例控制台 supervisor 的**安全关键接线**（同样从 run() 里抽出来，好真测，见 H6）。

    四条属性由 `tests/widget/test_m4_run_wiring.py` 行为锁住：
    - 重连必须拿 **`wechat_insts` 全集**做身份匹配。只拿本号一条去匹配的话，端口上乱报的任何
      wxid 都会被硬套到本号上 —— 别人在线的微信会被认领成这条渠道（M4 红线）。
    - `legacy` 由**启动时的整份配置**判一次后显式传进来，重连不许自己按「手里几条实例」重推：
      重推 = 恒判 legacy = 绕过整条认领链，直接往配置端口上的陌生微信写 g_IsLogin。
    - 掉线 → `park_offline_channel` 停掉那条可能已经在读别人微信的轮询。
    - 身份漂移（原地换号，进程还活着、内存采得到）→ health 判据把这行打回 offline 重跑认领链
      （H5）。没有它，这行会永远挂着 online，认领链再也不跑，渠道永久指着别人的账号。
    """
    from widget.instance_supervisor import InstanceSupervisor
    import time as _time
    _now = clock or _time.monotonic
    # 「昂贵身份确认」的时间表：ck -> 上次确认时刻，"*" = 全挂件上次确认时刻（见 _wechat_healthy）。
    _confirm_at: dict = {}
    inst_by_ck = {i.channel_key: i for i in insts}
    holder: dict = {}

    def _reconnect_wechat(inst_state) -> None:
        """重跑一次本号的认领。跑在 supervisor 后台线程，**绝不碰 Qt widget**。"""
        if injected_adapter is not None:
            return                      # 预注入 adapter 的路径没有可重新发现的进程
        if ready_gate is not None and not ready_gate():
            return                      # 轮询还没起：on_msg 未就绪、页面还没建好
        target = inst_by_ck.get(getattr(inst_state, "channel_key", ""))
        if target is None:
            return
        sup = holder.get("sup")
        # 别的号此刻正在用的端口：本轮重连绝不去认领它们（认领 = 两条渠道指向同一台微信，
        # 且会再往它写一次内存）。
        reserved = {st.port: st.channel_key for st in (sup.view() if sup else [])
                    if getattr(st, "platform", "") == "wechat" and st.port
                    and st.channel_key != inst_state.channel_key and st.status == "online"}
        # 重连成功后页面的重绑经 Qt 信号排队回 GUI 线程做（on_ready），本函数不碰任何 widget。
        # ★M5★ 把 pool 传下去 → 重连按本号自己的租户拿 Bridge（隔离），不回落到共享 bridge。
        (reconnect_fn or reconnect_wechat_instance)(
            inst_state, target, cfg, bridge, state, hub,
            all_insts=wechat_insts, reserved_ports=reserved, legacy=legacy,
            on_message=on_message, on_ready=on_ready, log=log, pool=pool)

    def _reconnect_wecom(inst_state) -> None:
        return          # 企微重连沿用 connector 自身的 connect_with_retry；M4 不改其行为

    def _on_instance_offline(inst_state) -> None:
        """某个微信号掉线/换号 → 立刻停掉它那条还在读那个端口的轮询。只动 hub，不碰 Qt。"""
        try:
            park_offline_channel(inst_state, hub, log=log)
        except Exception as e:                      # noqa: BLE001
            log(f"[自愈] [!] {getattr(inst_state, 'channel_key', '')} 掉线处理失败（忽略）：{e}")

    def _wechat_healthy(inst_state) -> bool:
        """★规则 3★ 周期性的昂贵确认（`open_files`，130~280ms **持 GIL**）只在这条后台线程上做
        —— 读路径一律只看结论（见 `WeChatHookAdapter.owns_its_account` 的 R2 注释）。

        而且**整个挂件每个 tick 最多做一次**，轮着来：
          · GIL 占空比 = 200ms / 5s ≈ 4%，**与实例数无关**（每实例每 tick 各探一次的话，
            6 开就是 1.2s/5s ≈ 24% —— 那等于把 GUI 卡顿从读路径搬到后台，没解决问题）；
          · 每条渠道大约每 `tick × 实例数` 秒被重新确认一次（6 开 ≈ 30s）。这只是**兜底**：
            真实的换号在 0.25s 内就被便宜触发器拦住（读写全拒），或在轮询遇到陌生会话表时
            当场确认（规则 1）—— 兜底是给「hook 一直乱报旧号 + 恰好没有陌生会话」那个角落准备的。
        存疑（便宜信号举了手、还没裁决）可以**插队**：那期间收发全拒，必须尽快给个结论。
        """
        ck = getattr(inst_state, "channel_key", "")
        if wechat_identity_suspect(hub, ck):
            return wechat_identity_healthy(hub, ck)          # 插队：事件驱动，不占时间表名额
        now = _now()
        n = max(1, len([st for st in (holder["sup"].view() if holder.get("sup") else [])
                        if getattr(st, "platform", "") == "wechat"]))
        mine, anyone = _confirm_at.get(ck), _confirm_at.get("*")
        due = mine is None or (now - mine) >= confirm_spacing_s * n
        if due and (anyone is None or (now - anyone) >= confirm_spacing_s):
            _confirm_at[ck] = _confirm_at["*"] = now         # 本 tick 的名额归这条渠道
            return wechat_identity_healthy(hub, ck)
        return not wechat_identity_drifted(hub, ck)          # 没轮到：纯读结论，零 syscall

    sup = (supervisor_factory or InstanceSupervisor)(
        state, sampler=sampler, logger=log,
        reconnect_fns={"wechat": _reconnect_wechat, "wecom": _reconnect_wecom},
        health_fns={"wechat": _wechat_healthy},
        on_offline=_on_instance_offline)
    holder["sup"] = sup
    return sup


def run(config_path: str = "widget_config.yaml", adapter=None) -> None:  # pragma: no cover
    import time
    import threading
    from PySide6.QtCore import QObject, Signal, QTimer
    from PySide6.QtWidgets import QApplication
    from widget.ui.workbench import WorkbenchWindow
    from widget.ui.theme import apply_theme
    from widget.ui.pages.broadcast_page import BroadcastPage
    from widget.ui.pages.broadcast_tasks_page import BroadcastTasksPage
    from widget.ui.tray import Tray
    from widget.ui.startup_splash import StartupSplash
    from widget.ui.app_icon import (apply_windows_window_icon, load_app_icon,
                                    set_windows_app_user_model_id)
    from widget.ui.window_activation import show_front
    from widget.handoff import HandoffController
    from widget.channel_hub import ChannelHub
    from widget.bridge import BridgeError
    from widget.single_instance import acquire
    from widget.broadcast.engine import run_broadcast
    from widget.broadcast import scheduler, models
    from widget.sender import RateLimiter

    BROADCAST_TASKS_PATH = "broadcast_tasks.json"

    if not acquire("AI-customer-service-widget"):
        print("[启动] 挂件已在运行（单实例锁），本次不重复启动。")
        return

    # 不再删除微信 crashinfo：隐藏“异常退出”提示不能修复原生崩溃，反而会销毁诊断证据。
    try:
        cfg = load_config(config_path)
    except Exception:
        cfg = WidgetConfig()
    state = RuntimeState(cfg, config_path=config_path)
    state.wechat_version = detect_wechat_version()

    set_windows_app_user_model_id()
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("AI客服")
    app.setApplicationDisplayName("AI客服")
    app_icon = load_app_icon()
    if not app_icon.isNull():
        app.setWindowIcon(app_icon)
    app.setQuitOnLastWindowClosed(False)   # 托盘程序：关闭窗口只收进托盘，不退出（从托盘「退出」才结束）
    apply_theme(app)
    startup = StartupSplash(app_icon)
    startup.set_stage("正在连接 AI 服务…")

    # 后台轮询线程不能直接碰 Qt widget：用信号排队到 GUI 线程刷新
    class _Signals(QObject):
        refresh = Signal()
        hook_error = Signal(str)
        channel_ready = Signal(str)   # 某渠道后台重连成功 → 排队回 GUI 线程重绑各页面的 adapter
        notification = Signal(str, str)  # 后台轮询线程只发信号；QSystemTrayIcon 必须在 GUI 线程操作
        open_handoff = Signal()          # 是否抢前台由用户的通知策略决定
        conversation_event = Signal(object)  # 收/发事件直接回显，不等待微信数据库落盘
    signals = _Signals()
    state.set_conversation_listener(signals.conversation_event.emit)

    # ★M5 多租户 N-Bridge 池★：每个多开账号用**自己 tenant/login 登录的独立 Bridge**，后端既有的
    # 按 token 租户作用域即隔离数据（后端零改）。去重键 (backend, tenant_id, login)：同租户账号共用
    # 一个 Bridge/token ⇒ legacy/单租户所有合成实例同键 → 池只造一个 = 与旧版 `Bridge(cfg)` 逐字节
    # 等价（零回归）；不同租户各自独立 Bridge ⇒ 隔离。逐账号登录挪到实例加载之后（见下 for pool.all()）。
    pool = BridgePool(cfg, log=print)
    bridge = pool.for_config(cfg)   # 基础/默认 Bridge（primary 未认领时副表盘回落；legacy 唯一那个）

    # state_path 持久化每会话游标：微信/挂件重启后从上次位置续传，既不重放历史也不漏消息
    # 实例注册表驱动：无 instances.yaml 时 load_instances 合成出与旧版完全等价的单条
    # wechat 实例（channel_key="wechat_personal"），循环体只跑一次 → 单实例行为零变化。
    # 坏 instances.yaml 绝不能让挂件起不来（pythonw 下 = 双击没反应）：统一走降级加载。
    insts = load_instances_or_degrade(cfg, log=print)
    hub = ChannelHub()
    # enabled=false 的行不接管；多实例走进程驱动认领（legacy 单实例路径逐字节不变）。
    wechat_insts = wechat_instances(insts)
    # ★legacy 只在这里、拿整份加载出来的配置判一次★，此后一路显式传递（启动、重连、状态行）。
    # 让下游按「手里这个列表有几条」重新推导过一次 —— 重连恒传单元素列表 → 恒判 legacy →
    # 绕过整条认领链往配置端口上的陌生微信写内存（M4 红线）。
    wechat_legacy = is_legacy_wechat(wechat_insts)
    injected_adapter = adapter   # run() 的 adapter 入参：测试/预注入单实例路径

    startup.set_stage("正在识别已登录的微信…")

    # 挂件自管拉起（多号）：冷启动且一个微信都没跑时，按正确顺序拉整队——第一个 StartPort=30002、
    # 后续无参数→30001（真机铁律，见 widget/wechat/launcher.py）。**只在干净起点自动拉**：已有微信
    # 在跑就不插手（交给「新建」按钮/supervisor 重连），彻底避开「已有实例占着 30001」的撞车。
    # 拉起只开登录窗，扫码是人工步；未登录的号本轮 discovery 会 park，扫完由 supervisor 重连认领。
    # ★打包版单号也要自动拉★：过去这里写的是 `not wechat_legacy`，把「没有 instances.yaml 的
    # 合成单实例」（= 绝大多数打包交付的客户）整条排除在自管拉起之外 —— 客户双击挂件、微信没开，
    # 于是永远「未连接」。现在两条分支共用**同一个** plan_wechat_autolaunch 决策（决不会重复拉，
    # 且「有微信进程就不拉」—— hook 被自动更新覆盖 / 端口还没绑上时都不会再弹一个微信）。
    if injected_adapter is None and wechat_insts:
        try:
            from widget.wechat.launcher import (DEFAULT_HOOK_PORT, launch_wechat_fleet,
                                                launch_wechat_instance)
            from widget.wechat.ports import listening_port_for_pid, wechat_pids as _wxpids
            plan = plan_wechat_autolaunch(legacy=wechat_legacy, log=print)
            if plan == "legacy":
                # legacy 单号必须**无参数**拉起 → hook 落 30001，正是 legacy 配置里
                # hook_base_url 指的端口。走 plan_fleet 会给第一个实例 StartPort=30002，
                # 挂件连的却还是 30001 → 拉起了也接不上（比不拉更糟）。
                launch_wechat_instance(start_port=None, hook_port=DEFAULT_HOOK_PORT, log=print)
            elif plan == "fleet":
                def _wait_up() -> None:
                    for _ in range(30):                 # 等新实例起来（≤15s）再拉下一个
                        if any(listening_port_for_pid(p) for p in _wxpids()):
                            return
                        time.sleep(0.5)
                launch_wechat_fleet(n_want=len(wechat_insts), n_running=0,
                                    wait_up=_wait_up, log=print)
            if plan:
                for _ in range(20):                     # 拉完略等端口就绪，再交给 discovery
                    if any(listening_port_for_pid(p) for p in _wxpids()):
                        break
                    time.sleep(0.5)
        except Exception as e:                          # noqa: BLE001 —— 自管拉起失败绝不挡启动
            print(f"[启动] [!] 微信自管拉起失败（忽略，可用『新建』按钮手动拉）：{e}")

    # legacy 客户可能手工多开了两个无参数微信，二者会用 SO_REUSEADDR 共同监听 30001。
    # 用户选择后不退出、也不重开任何微信。若共享端口把所有连接都交给另一个 PID，就只把所选
    # hook 的监听 socket 热迁移到空闲端口；微信进程和登录态保持不动。随后每条 HTTP 连接仍按
    # 系统 TCP 表核对服务端 PID，避免把会话或消息请求送到其他账号。
    runtime_injected_adapter = injected_adapter
    if injected_adapter is None and wechat_legacy and wechat_insts:
        try:
            from widget.instances import effective_hook_port
            from widget.ui.wechat_instance_picker import choose_wechat_instance
            select_port = int(effective_hook_port(wechat_insts[0]) or 0)
            wechat_legacy, selected_wechat, selection_error = prepare_legacy_wechat_selection(
                wechat_insts, legacy=wechat_legacy, port=select_port,
                chooser=choose_wechat_instance,
            )
            if selected_wechat is not None:
                selected_port = int(getattr(selected_wechat, "hook_port", 0) or select_port)
                runtime_injected_adapter, patch_status = build_routed_wechat_adapter(
                    cfg, wechat_insts[0], selected_wechat, selected_port,
                )
                routed_port = int(runtime_injected_adapter.hook_port() or selected_port)
                print(f"[启动] 用户选择接管个人微信 pid={selected_wechat.pid} "
                      f"account={selected_wechat.wxid} port={routed_port}；"
                      "已启用独立端口与连接级 PID 校验，所有微信保持在线")
                print(f"[启动] 所选微信登录补丁：{patch_status}")
            elif selection_error:
                print(f"[启动] [!] {selection_error}；个人微信保持未接管")
                return
        except Exception as e:                          # noqa: BLE001 —— 选择器失败绝不猜账号
            print(f"[启动] [!] 个人微信选择流程失败（{e}）：本次不猜测接管对象")
            return

    # 多微信时，账号选择器必须是用户看到的第一个界面；确认接管后才显示启动进度，避免把
    # “AI 客服工作台”误解成已经接管完成。后端登录也放到选择之后，选择器不会再被网络等待挡住。
    startup.show()
    startup.set_stage("正在连接所选账号与 AI 服务…")

    # ★M5★ 逐账号登录：先按**全部实例**扇出填池（同 (tenant,login) 去重），再对去重后的每个
    # Bridge 各登录一次（每租户一次，不做 N 次）。legacy/单租户所有合成实例同键 → 池仅一个 Bridge
    # → 与旧版 `bridge.login()` 逐字节等价。某租户登录失败 → 该租户 Bridge token 空、其渠道 chat 走
    # 既有 BridgeError→转人工降级，**绝不回落到别租户的 Bridge**（隔离铁律）；登录后校验 JWT 租户。
    for _i in insts:
        pool.for_instance(_i)                       # 填池（去重）
    for _b in pool.all():
        _bcfg = getattr(_b, "cfg", cfg)
        try:
            _b.login()
            pool.warn_if_tenant_mismatch(_bcfg, getattr(_b, "token", "") or "")
            print(f"[启动] 后端登录成功 tenant={getattr(_bcfg, 'tenant_id', '')} "
                  f"login={getattr(_bcfg, 'login', '')}")
        except BridgeError as e:
            print(f"[启动] [!] 后端登录失败({e}) tenant={getattr(_bcfg, 'tenant_id', '')} "
                  f"login={getattr(_bcfg, 'login', '')}：该渠道消息将转待人工，不会自动回复")
    state.backend_ok = bool(getattr(bridge, "token", ""))
    state.backend_error = "" if state.backend_ok else "后端登录失败"

    adapter, _pipe, wechat_pids, wechat_ports, _wechat_parked = start_wechat_channels(
        wechat_insts, cfg, bridge, state, hub, legacy=wechat_legacy,
        all_insts=wechat_insts, injected_adapter=runtime_injected_adapter, log=print, pool=pool)

    # 多渠道注册中心：个人微信已在上面循环里注册；企微额外接进来（双渠道同跑）。
    # M2：instances.yaml 配了 >1 个企微账号时，用 WeComManager 扇出所有 WXWork 进程；
    # legacy（无 instances.yaml，只有合成的单条 wecom_hook）则保持原单路径，零回归。
    wecom_adapters: list = []
    wecom_syncers: list = []
    wecom_pids: dict = {}          # channel_key -> pid（多企微路径才有；供 M3 supervisor 采内存用）
    wecom_insts = [i for i in insts if i.platform == "wecom"]
    # ★接线前先把企微客户端打开★：打包客户冷启动时企微没开 → 注入扑空 → 企微整条渠道不可用。
    # 只开客户端（扫码/登录仍是人工步），已有企微在跑就不插手；失败只打日志，绝不挡启动。
    startup.set_stage("正在连接微信与企业微信渠道…")
    prelaunch_wecom(wecom_insts, log=print)
    is_legacy_wecom = (len(wecom_insts) <= 1) and (
        not wecom_insts or wecom_insts[0].channel_key == "wecom_hook")
    if is_legacy_wecom and Path("wecom_hook_config.yaml").exists():
        try:
            from widget.wecom.config import WeComHookConfig
            from widget.wecom.runtime import create_wecom_runtime
            wecom_cfg = WeComHookConfig.load("wecom_hook_config.yaml")
            # ★M5/D5★ 企微用**它自己那条实例的租户 Bridge**（否则 B 号历史蒸馏进 A 租户 KB）。
            # legacy 单企微 = 与 cfg 同 (tenant,login) → 池里就是 base bridge，行为等价。
            wecom_bridge = pool.for_instance(wecom_insts[0]) if wecom_insts else bridge
            wecom_adapter, wecom_pipe = create_wecom_runtime(
                cfg, wecom_bridge, state, wecom_cfg, logger=lambda m: print(m))
            hub.register("wecom_hook", wecom_adapter, wecom_pipe)
            print(f"[启动] 企微渠道已接入 self_id={wecom_cfg.self_id or '(未配置)'}")
            wecom_adapters.append(wecom_adapter)
            # 企微本地库历史【自动】反哺：后台守护线程持续收割→蒸馏入库，客户无需点任何按钮。
            if wecom_cfg.auto_history_sync:
                from widget.wecom.history_sync import WeComHistorySyncer
                wecom_syncers.append(WeComHistorySyncer(
                    wecom_adapter, wecom_bridge,
                    poll_interval_s=wecom_cfg.history_sync_interval_s,
                    flush_interval_s=wecom_cfg.history_flush_interval_s,
                    min_batch=wecom_cfg.history_min_batch, logger=lambda m: print(m)))
        except Exception as e:      # 企微接不上绝不影响个人微信主路径
            print(f"[启动] [!] 企微渠道未接入（{e}）：仅个人微信运行")
    elif wecom_insts:
        # 多企微账号：WeComManager 枚举所有 WXWork.exe 进程，逐个 connect 拿 self_id 去匹配
        # instances.yaml 里的账号行；命中的接入 hub（各自 channel_key），未配置的账号 park（不误绑租户）。
        try:
            from widget.wecom.manager import WeComManager
            from widget.wecom.config import WeComHookConfig
            from widget.wecom.history_sync import WeComHistorySyncer
            bridges = {i.channel_key: pool.for_instance(i) for i in wecom_insts}   # ★M5★ 每账号自己租户的 Bridge（隔离）
            # 真实 wecom 配置（含 wework_exe_path）喂给 manager → connector 版本闸不被跳过，
            # 绝不用裸 WeComHookConfig() 硬注（"绝不硬注写崩企微"）。
            wecom_cfg = WeComHookConfig.load("wecom_hook_config.yaml")
            mgr = WeComManager(wecom_insts, cfg, bridges, state, hub,
                                wecom_cfg=wecom_cfg, logger=lambda m: print(m))
            mgr.start()
            wecom_pids = dict(mgr.pids)
            default_wecom_cfg = WeComHookConfig()
            for ck, ad in mgr.adapters.items():
                wecom_adapters.append(ad)
                print(f"[启动] 企微渠道已接入 channel={ck}")
                if default_wecom_cfg.auto_history_sync:
                    # ★D5★ 反哺用**该账号自己渠道的** Bridge（B 号历史进 B 租户 KB，不串 A）。
                    wecom_syncers.append(WeComHistorySyncer(
                        ad, bridges.get(ck, bridge),
                        poll_interval_s=default_wecom_cfg.history_sync_interval_s,
                        flush_interval_s=default_wecom_cfg.history_flush_interval_s,
                        min_batch=default_wecom_cfg.history_min_batch, logger=lambda m: print(m)))
            if mgr.parked:
                print(f"[启动] [!] 未配置账号的企微进程已 park（不接入）：{mgr.parked}")
        except Exception as e:      # 企微多实例接不上绝不影响个人微信主路径
            print(f"[启动] [!] 企微多实例未接入（{e}）：仅个人微信运行")

    # M4 多实例控制台：health/内存后台采样 + 一键新建入口 + **真重连**。
    # M3 遗留（本任务修）：reconnect_fns 是空壳 —— 微信掉线只标记 offline，永远不自己回来，
    # 只能靠客户重启挂件。现在掉线/park 的号由 supervisor 按退避重跑一次
    # 「发现 → 认领 → patch → 注册 → 起轮询」，取代旧的 `_try_reconnect_hook`（只会重连
    # 那一个 legacy wechat_personal，且盲信 30001）。
    from widget.instance_supervisor import InstanceState
    _inst_by_ck = {i.channel_key: i for i in insts}
    _polling_started = threading.Event()   # start_all 之前不重连：on_msg 未定义、页面还没建好

    # 重连/掉线/身份漂移这三条**安全关键**接线抽在模块级 `build_instance_supervisor` 里，
    # 由 tests/widget/test_m4_run_wiring.py 做真行为测试（run() 本身没有行为覆盖，见 H6）。
    # `on_message` 用 lambda 延迟解析：`on_msg` 在下面才定义。
    sup = build_instance_supervisor(
        hub, cfg, bridge, state, insts=insts, wechat_insts=wechat_insts,
        legacy=wechat_legacy, on_message=lambda m: on_msg(m),
        on_ready=signals.channel_ready.emit, ready_gate=_polling_started.is_set,
        injected_adapter=runtime_injected_adapter, log=print, pool=pool)
    for ad in wecom_adapters:
        try:
            ck = getattr(ad, "channel", "")
            ic = _inst_by_ck.get(ck)
            sup.register(InstanceState(
                channel_key=ck, platform="wecom",
                account_id=(ic.account_id if ic else ck),
                display_name=(ic.display_name if ic else ck),
                pid=wecom_pids.get(ck, 0), status="online", mem_mb=0, ai_enabled=True))
        except Exception as e:
            print(f"[启动] [!] 实例控制台登记企微渠道失败（忽略）：{e}")
    try:
        # 每个号登记自己的 pid/端口（认领到的用 manager 给的，未认领的按本行端口现场探一次）。
        for st in wechat_instance_states(wechat_insts, wechat_pids, wechat_ports,
                                         legacy=wechat_legacy):
            sup.register(st)
    except Exception as e:
        print(f"[启动] [!] 实例控制台登记微信渠道失败（忽略）：{e}")
    sup.start()

    # M3 转人工：控制器（多渠道，按 channel 路由回复）。人工发消息/AI 托管开关都在「会话」页里。
    controller = HandoffController(hub, state)

    # 头像：从 hook 取微信头像（本地缓存优先 / URL 兜底 / 首字母占位），各页共用。
    # M4：头像/群发/聊天记录这些副表盘钉「主实例」= 第一个被认领的微信号，用它**真实发现的
    # 端口**，而不是配置里写死的 30001（多开时 30001 可能根本不是主号）。核心接待环
    # （收→AI→回）N 个号全通；副功能 M4 只服务主号。
    # 从 `adapter`（下游各页真正用来发消息的那个微信 adapter）反查它的端口，而不是从
    # `wechat_pids` 的迭代顺序猜 —— 联系人源/头像必须和发送账号是同一个号。
    primary_base_url = primary_wechat_base_url(adapter, wechat_ports, cfg)
    state.hook_base_url = primary_base_url    # 状态页显示主实例的真实 hook 地址
    # ★H3★ 副表盘不许自己捧着 `primary_base_url` 这个**启动时捕获的端口字符串**：微信重启后
    # 端口易主（甲退出→乙抢到 30001，甲被重新认领到 30002），那就是在读陌生人的微信库 ——
    # 群发页列出乙的私人好友、由甲的微信发出去，头像页摆陌生人的头像。改成每次读之前现取
    # hub 里**此刻**注册的那个 adapter，查询走它带闸的 query_db；取不到就空表/占位。
    _live_wechat_adapter, _wechat_contacts, avatars = build_wechat_side_panels(
        hub, getattr(adapter, "channel", ""), cfg)

    # 会话/群发页优先展示已接管的个人微信；个人微信确实不可用时才回退企微。
    # 两条渠道仍都进入 history_channels，用户可通过“渠道”下拉主动切换。
    use_wecom_pages, pages_adapter = choose_pages_adapter(adapter, wecom_adapters)
    pages_ck = getattr(pages_adapter, "channel", "")   # 各页绑的是哪条渠道（重连后按它重绑）

    # 群发：联系人源——企微渠道用 WeComContactSource（出所有企微联系人），否则个人微信 contact.db
    bcfg = cfg.broadcast
    if use_wecom_pages:
        from widget.wecom.contact_source import WeComContactSource
        contact_source = WeComContactSource(wecom_adapters[0])
        print("[启动] 群发/聊天记录页用企微渠道（个人微信 hook 未就绪）")
    else:
        # 收件人名单必须读**发送那个号**的 contact.db，而且是**此刻**那个号
        # （端口会易主、账号会原地换、重连会换 adapter）—— 见 build_wechat_side_panels。
        contact_source = _wechat_contacts
    bc_limiter = RateLimiter(bcfg.rate_per_min, bcfg.daily_limit)
    # 渠道 key → 显示名（会话页下拉、实例控制台、群发标题共用）
    _CHANNEL_LABELS = {i.channel_key: i.display_name for i in insts}
    # spec §6：群发这类副表盘钉「主实例」，但标题必须写明是哪个号在发 —— 群发是真在花钱的，
    # 发错号 = 发错客户，不能装作它是全局的。
    primary_label = _CHANNEL_LABELS.get(getattr(pages_adapter, "channel", ""), "")
    # ★M5/D5 审查修复★ 副表盘（群发草稿 / 工作台画像·标签·ai-mute）是进程级单面板，它真正
    # 操作的是 **`pages_ck` 那条渠道**（当前默认渠道，与 pages_adapter/contact_source/
    # primary_label 一致）。所以它持有的 Bridge/token 必须是 pages_ck 那条渠道的租户，绝不能
    # 钉到个人微信 adapter 那条——否则 summarize_to_kb 把该客户历史蒸馏进错租户 KB、set_ai_mute
    # 写错租户静默失效（见 side_panel_bridge 文档）。该渠道未接上则回落基础/默认 Bridge。
    primary_bridge = side_panel_bridge(pool, _inst_by_ck, pages_ck, cfg)
    memory_bridge_for = lambda channel_key: side_panel_bridge(
        pool, _inst_by_ck, channel_key, cfg
    )
    bpage = BroadcastPage(primary_bridge, contact_source, bcfg, pages_adapter,
                          tasks_path=BROADCAST_TASKS_PATH, limiter=bc_limiter, signals=signals,
                          avatar_provider=avatars, account_label=primary_label)

    # 群发任务清单页：「继续/重试」委托给群发页执行并切过去看进度
    def _on_run_task(task, mode):
        if mode == "resume":
            bpage.resume_task(task)
        else:
            bpage.retry_task(task)
        wb.show_broadcast()
    tpage = BroadcastTasksPage(BROADCAST_TASKS_PATH, on_run_task=_on_run_task,
                               source=contact_source)

    # 会话页渠道切换：把 hub 里已注册的所有渠道（个人微信 + 企微）给会话页；默认选当前主渠道
    # （个人微信可用时默认个人微信，否则回退企微）。只有一条渠道时会话页不显示下拉。
    history_channels = [(ch, _CHANNEL_LABELS.get(ch, ch)) for ch in hub.channels()]
    history_default = getattr(pages_adapter, "channel", "")
    startup.set_stage("正在准备会话中心…")

    def _new_instance_flow(platform: str) -> None:
        """「新建实例」：拉起新客户端进程 + 提示扫码；绝不抛进 GUI（best-effort）。

        微信（挂件自管拉起）：`StartPort=` 只有第一个实例吃得下，后续实例必须无参数（真机
        2026-08-03 铁律）。所以按当前在跑的实例数决定：0 个→`StartPort=30002`、已有→无参数→30001。
        一台机最多 2 个（`MAX_WECHAT_INSTANCES`）。不走 multiopen.exe。
        企微：强制单实例，仍走 multiopen.exe 关互斥。

        扫码后由 supervisor 的重连回调重跑认领 —— 前提是该号的 self_wxid 已写进 instances.yaml。
        """
        try:
            if platform == "wechat":
                from widget.wechat.launcher import launch_wechat_instance, plan_fleet
                from widget.wechat.ports import listening_port_for_pid, wechat_pids
                running = sum(1 for p in wechat_pids() if listening_port_for_pid(p))
                plan = plan_fleet(running + 1, running)     # 再加一个的 (start_port, hook_port)
                if not plan:
                    print("[启动] [!] 已达上限 2 个微信实例（真机确认），不再新建")
                    return
                start_port, hook_port = plan[0]
                pid = launch_wechat_instance(start_port=start_port, hook_port=hook_port, log=print)
                if pid:
                    mode = f"StartPort={start_port}" if start_port else "无参数"
                    print(f"[启动] 新微信实例已拉起 pid={pid}（{mode}，hook 落点 {hook_port}），"
                          f"请在新窗口扫码登录")
                else:
                    print("[启动] [!] 新建微信实例未成功拉起")
                return
            from widget.multiopen import launch_new_instance
            exe = "native/build/out/multiopen.exe"
            if platform == "wecom":
                pid = launch_new_instance(exe, "WXWork.exe", launch_wecom=True,
                                          mutex_substr="ExclusiveObject", close=True, log=print)
            else:
                pid = None
            if pid:
                print(f"[启动] 新实例已拉起 pid={pid}，请在新窗口扫码登录")
            else:
                print(f"[启动] [!] 新建 {platform} 实例未成功拉起（见上方 multiopen 日志）")
        except Exception as e:
            print(f"[启动] [!] 新建实例失败（忽略，不影响现有实例）：{e}")

    from widget.ui.pages.instances_page import InstancesConsolePage
    # on_open_chat 的 lambda 此刻引用的 wb 尚未赋值，但闭包按名字延迟解析——真正被点击调用时
    # （用户已看到工作台）wb 早已在下面一行赋好，不会是 None/未定义。
    console = InstancesConsolePage(sup, on_open_chat=lambda ck: wb.show_history_for_channel(ck),
                                   on_new_instance=_new_instance_flow)
    # 版本页：微信/企微版本 + 可注入判定 + 强制降级（降级在后台线程跑，页面自己管确认与忙态）。
    # 建页失败绝不能挡工作台起来 —— 顶多少一页。
    try:
        from widget import downgrade as _downgrade
        from widget import version_check as _version_check
        from widget.ui.pages.version_page import VersionPage
        vpage = VersionPage(
            status_fn=_version_check.status,
            downgrade_wechat_fn=make_downgrade_fn(_downgrade.downgrade_wechat, "微信", log=print),
            downgrade_wecom_fn=make_downgrade_fn(_downgrade.downgrade_wecom, "企业微信", log=print),
            log=print)
    except Exception as e:                              # noqa: BLE001
        print(f"[启动] [!] 版本页未接入（忽略）：{e}")
        vpage = None
    wb = WorkbenchWindow(state, controller, primary_bridge, broadcast_page=bpage, tasks_page=tpage,
                         avatar_provider=avatars, adapter=pages_adapter,
                         history_tasks_path=BROADCAST_TASKS_PATH, history_source=contact_source,
                         history_channels=history_channels, history_adapter_for=hub.adapter,
                          memory_bridge_for=memory_bridge_for,
                          history_default_channel=history_default, instances_page=console,
                          version_page=vpage, labels=_CHANNEL_LABELS, app_icon=app_icon)
    bpage.on_open_chat = wb.show_history      # 群发好友双击 → 跳该好友会话
    signals.refresh.connect(wb.refresh)
    signals.conversation_event.connect(wb.history_page.apply_live_event)
    signals.conversation_event.connect(wb.memory_conversation_page.apply_live_event)
    tray = Tray(state, on_open=lambda: show_front(wb), on_quit=app.quit,
                on_handoff=wb.show_handoff, app_icon=app_icon)
    tray.show()
    signals.notification.connect(tray.notify)
    signals.open_handoff.connect(wb.show_handoff)
    from widget.notifications import NotificationGate
    notification_gate = NotificationGate(cfg.notifications)
    show_front(wb)              # 启动即显示工作台（托盘图标可能被 Windows 折叠，主动弹窗更直观）
    startup.finish(wb)
    # setWindowIcon 在 HWND 创建前已设置一次；窗口显示后再写 Qt/Win32 大小图标槽，处理 Windows
    # 任务栏仍缓存 pythonw.exe 通用图标的情况。延迟 500ms 再补一次覆盖 Explorer 的首帧缓存。
    apply_windows_window_icon(wb, app_icon)
    QTimer.singleShot(500, lambda: apply_windows_window_icon(wb, app_icon))

    def _set_tray_health() -> None:
        tray.set_health(ok=(state.hook_ok and state.backend_ok),
                        degraded=not (state.hook_ok and state.backend_ok))

    def _mark_hook_error(message: str) -> None:
        state.hook_ok = False
        state.hook_error = message
        wb.status_page.refresh()
        _set_tray_health()

    def _on_channel_ready(ck: str) -> None:
        """后台重连成功 → 回到 GUI 线程把待人工/群发/聊天记录三页重绑到新 adapter。"""
        try:
            if rebind_pages_after_reconnect(
                    ck, hub.adapter(ck), pages_channel_key=pages_ck,
                    handoff_page=wb.handoff_page, broadcast_page=bpage,
                    history_page=wb.history_page, contact_source=contact_source,
                    avatar_provider=avatars,
                    on_refresh=lambda: (wb.refresh(), _set_tray_health())):
                print(f"[自愈] {ck} 页面已重绑到新 adapter")
        except Exception as e:      # noqa: BLE001 —— 重绑失败绝不能把 GUI 打崩
            print(f"[自愈] [!] {ck} 页面重绑失败（忽略）：{e}")

    signals.hook_error.connect(_mark_hook_error)
    signals.channel_ready.connect(_on_channel_ready)

    # 定时群发调度：每 60s 检查一次到期任务；错过的（开机时已过点）首个 tick 就补发，并托盘提示
    def _fire_due_task(task_id: str) -> None:
        def worker() -> None:
            all_tasks = models.load_tasks(BROADCAST_TASKS_PATH)
            task = next((t for t in all_tasks if t.id == task_id), None)
            if task is None:
                return

            # 单条 upsert，不整表覆盖：UI 页可能同时在推进别的任务并落盘，
            # 这里绝不能用自己手里这份旧快照把对方刚写下的进展打回去。
            try:
                # 发送账号必须与 contact_source 是同一个号（群发页用的也是 pages_adapter）：
                # 用 `adapter` 会在企微在场时拿企微的名单从个人微信发出去。
                run_broadcast(task, contact_source, pages_adapter, bcfg,
                             limiter=bc_limiter, should_stop=lambda: False,
                             save_fn=lambda t: models.upsert_task(BROADCAST_TASKS_PATH, t))
            except Exception as e:
                # 无人值守的唯一执行路径：不兜住会导致任务卡死在 running（送达一半不再重试）
                # 或反复卡在 scheduled 被下个 60s tick 重新 fire、反复刷屏 tray 提示。
                # 落地为 paused：既不会被 due_tasks 再次挑中，也不是终态，商家可在 UI 里查看/重试。
                signals.notification.emit("群发任务执行失败", f"任务 {task.id[:8]}：{e}"[:60])
                task.status = "paused"
                models.upsert_task(BROADCAST_TASKS_PATH, task)
        threading.Thread(target=worker, daemon=True).start()

    def _check_due_tasks() -> None:
        tasks = models.load_tasks(BROADCAST_TASKS_PATH)
        for t in scheduler.due_tasks(tasks, time.time()):
            tray.notify("定时群发", f"任务到点，开始发送（共 {len(t.recipients)} 人）")
            _fire_due_task(t.id)

    sched_timer = QTimer()
    sched_timer.timeout.connect(_check_due_tasks)
    sched_timer.start(60_000)
    _check_due_tasks()          # 启动时立即查一次：错过的定时任务不用等下一个 60s

    # 内置自愈：每 10s 后台确保 g_IsLogin=1。微信重启会把注入 DLL 的补丁冲掉、QueryDB 读不到库
    # （表现为"收不到数据"）；有这个就不必再单独跑 `python -m widget.hook_patch --watch`。
    def _repatch() -> None:
        def worker():
            try:
                # 逐实例按各自端口补（sup.view() 里带着每个号的 port）——多开时不能只补 30001。
                repatch_wechat(sup.view(), log=print)
            except Exception:
                pass
        threading.Thread(target=worker, daemon=True).start()

    patch_timer = QTimer()
    patch_timer.timeout.connect(_repatch)
    patch_timer.start(10_000)

    # 单向跟随 + 聊天记录实时刷新：每 3s。**只在「聊天记录」页可见时才跑**——
    # live_tick 是个人微信 hook 的同步读取，在别的页面（如会话控制台）上每 3s 跑会白卡主线程
    # （个人微信 hook 不活时尤其明显，DEVLOG 记的主线程阻塞隐患）。
    def _follow_tick():
        try:
            if wb.current_page_name() == "history":
                wb.history_page.live_tick()
        except Exception:
            pass
    follow_timer = QTimer()
    follow_timer.timeout.connect(_follow_tick)
    follow_timer.start(3_000)

    # 实例控制台内存/在线状态实时刷新：每 5s。console.refresh() 只读 supervisor 的缓存视图
    # （view()/total_mem_mb()，后台线程已采好，无 I/O）→ GUI 线程直接调安全；仍只在「实例」
    # 页可见时跑，省下不必要的表格重建。不刷的话该页永远是 __init__ 时的一次性快照
    # （全 online/mem=0），supervisor 的实时采样和内存告警条形同虚设。
    def _instances_tick():
        try:
            if wb.current_page_name() == "instances":
                console.refresh()
        except Exception:
            pass
    instances_timer = QTimer()
    instances_timer.timeout.connect(_instances_tick)
    instances_timer.start(5_000)

    def on_msg(m):
        # 入站先回显，再进入可能耗时数秒的后端/LLM 链路；客户消息不再被 AI 推理时间拖住。
        signals.conversation_event.emit({
            "event_id": f"in:{m.get('channel', '')}:{m.get('msg_id', '')}",
            "direction": "inbound",
            "channel": m.get("channel", ""),
            "contact_id": m.get("contact_id", ""),
            "sender_id": m.get("sender_id", ""),
            "text": m.get("text", ""),
            "timestamp": int(m.get("timestamp") or 0),
            "is_group": bool(m.get("is_group", False)),
            "provenance": "customer",
        })
        action = hub.handle(m)     # 按 m["channel"] 路由到对应渠道的 pipeline
        if action == "ignored":
            # 未选择/黑名单/系统消息只推进 hook 游标，不打印私人正文、不刷新整套 UI。
            # 这一步是 selected 模式真正降低采集面和主线程负担的关键。
            return
        label = {"auto_reply": "自动回复[OK]", "auto_reply_draft": "AI已回答（自动发送关闭）",
                 "handoff": "转待人工",
                 "error": "后端出错→待人工"}.get(action, action)
        print(f"[收] {m['sender_id']} @{m['contact_id']}: {m['text']!r}  ->  {label}")
        channel = m.get("channel", "")
        if notification_gate.should_notify(action, channel, m["contact_id"]):
            if action in ("handoff", "error"):
                signals.notification.emit("有新的待人工消息",
                                          f"{m['sender_id']}：{m['text'][:20]}")
                if cfg.notifications.bring_to_front:
                    signals.open_handoff.emit()
            else:
                signals.notification.emit("AI 已自动处理",
                                          f"{m['sender_id']}：{m['text'][:20]}")
        signals.refresh.emit()     # 线程安全：queued connection 到 GUI 线程

    # 会话首轮读取先发车：当前已登录微信已经在上面完成安全路由和登录补丁，先读 session.db，
    # 再让消息基线扫描进入同一把 WCDB 锁。这样启动后的会话列表不必排在逐表基线之后。
    # 线程读完后信号会排队，Qt 事件循环启动即渲染；这里不阻塞主线程。
    wb.history_page.live_tick()

    # 起**全部**渠道的轮询（N 个微信 + N 个企微）。M4 之前只起「第一个」微信 adapter，
    # 实例 2..N 注册进了 hub 却从不轮询 —— 配了也收不到消息。企微不再单独起一遍：
    # start_all 已覆盖，重复 start() 会给同一个号开两条轮询线程（重复回复）。
    started = hub.start_all(on_msg)
    for ch, err in started.items():
        if err:
            print(f"[启动] [!] 渠道 {ch} 轮询未启动（{err}）")
    _polling_started.set()          # 此后 supervisor 的重连回调才允许接管（on_msg 已就绪）
    live = [ch for ch, err in started.items()          # 降级 adapter 的 start() 是空操作，不算「在跑」
            if not err and not isinstance(hub.adapter(ch), UnavailableWeChatAdapter)]
    if live:
        print(f"[启动] 开始轮询 {len(live)} 条渠道（每 {cfg.poll_interval_s}s）："
              f"{'、'.join(live)}。关闭窗口/Ctrl+C 退出。")
    else:
        print("[startup] 没有可轮询的渠道；supervisor 会按退避重试认领")

    # 预取线程可能因 hook 瞬时未就绪先返回失败；事件循环启动后立刻再异步读一次作为恢复兜底。
    QTimer.singleShot(0, wb.history_page.live_tick)

    for sy in wecom_syncers:                      # 启动历史【自动】反哺后台线程（客户无需点击）
        try:
            sy.start()
        except Exception as e:
            print(f"[启动] [!] 企微历史反哺未启动（{e}）")

    wb.refresh()
    _set_tray_health()
    app.exec()
    sup.stop()                 # 停实例控制台后台健康采样线程
    for sy in wecom_syncers:
        sy.stop()    # 停历史自动反哺后台线程
    hub.stop_all()             # 停所有渠道（个人微信 + 企微）
