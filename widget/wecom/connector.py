"""WeComConnector —— 企微自研 hook 的注入生命周期状态机（对照 WeiClaw connectPipeline）。

职责分工：connector 管【进程探测 / 版本闸 / 注入 / 就绪轮询 / 硬化】，
adapter 管【消息收发】。二者分开，别塞进 adapters/。

【今晚骨架只跑到 READY】：DETECTING → (版本闸) → INJECTING → 轮询 /health 就绪 → READY。
WAITING_LOGIN / HARDENING / 版本治理卸载重装 均留桩（走路骨架不做）。

WeiClaw 教训落地：
- 注入器退出码 0 ≠ 服务就绪 —— 就绪一律靠 BridgeClient 轮询 /health 判定。
- 单飞互斥（Lock）防 GUI 误触并发注入。
- 任一步异常 → state=FAILED，绝不崩挂件（上层显示"企微未连接"）。
"""
from __future__ import annotations

import secrets
import subprocess
import threading
import time
from enum import Enum
from typing import Callable

from widget.wecom.adapter import WeComHookAdapter
from widget.wecom.bridge_client import BridgeClient
from widget.wecom.config import WeComHookConfig
from widget.wecom.msgdb import MsgDbReader, WeComHistoryReader
from widget.wecom import offsets as offsets_mod
from widget.wecom import status_file
from widget.wecom import winproc


def _default_status_reader(pid: int, timeout_s: float) -> int | None:
    """默认从 status 文件学 DLL 实际绑定端口（多实例：各 pid 各自端口，非静态 8752）。"""
    return status_file.wait_ready(pid, timeout_s=timeout_s)


class ConnState(str, Enum):
    IDLE = "idle"
    DETECTING = "detecting"
    VERSION_CHECK = "version_check"
    VERSION_MISMATCH = "version_mismatch"
    INJECTING = "injecting"
    WAITING_LOGIN = "waiting_login"     # 骨架未用
    READY = "ready"
    HARDENING = "hardening"             # 骨架 no-op
    CONNECTED = "connected"
    FAILED = "failed"
    STOPPED = "stopped"


class WeComConnectError(RuntimeError):
    def __init__(self, state: ConnState, reason: str):
        super().__init__(f"[{state.value}] {reason}")
        self.state = state
        self.reason = reason


# 默认注入器 runner：跑子进程，返回退出码；stdout/stderr 收进 log。
def _default_injector_run(args: list[str], log: Callable[[str], None]) -> int:
    flags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        flags = subprocess.CREATE_NO_WINDOW           # 不弹黑窗
    proc = subprocess.run(args, capture_output=True, text=True, timeout=60, creationflags=flags)
    out = (proc.stdout or "") + (proc.stderr or "")
    for line in out.splitlines():
        log(f"[injector] {line}")
    return proc.returncode


def _default_harden(cfg, log: Callable[[str], None]) -> None:
    """默认硬化：**仅**删企微开机自启（HKCU Run），best-effort、非致命。

    刻意不学 WeiClaw 的激进做法：**绝不卸载用户机器上的企微**（governVersion 弹 UAC 强卸），
    **绝不双标自动更新**（关企微更新却让自己 autoDownload 全开）。删自启的目的只是减少企微自升级
    在后台悄悄把注入冲掉的概率；删错/删不到都无害（reg delete 不存在的值只是报错，被吞掉）。
    ⚠️ 改注册表，需真机验证；故由 cfg.harden_disable_autostart 显式开启，默认关。
    """
    run_key = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
    for name in ("WXWork", "WeCom", "企业微信", "WXWorkWeb"):
        try:
            subprocess.run(["reg", "delete", run_key, "/v", name, "/f"],
                           capture_output=True, text=True, timeout=10)
        except Exception as e:                          # noqa: BLE001 —— 删不掉不该影响连接
            log(f"[wecom-connector] 删自启 {name} 失败（忽略）：{e}")
    log("[wecom-connector] 硬化：已尝试移除企微开机自启（HKCU Run）")


class WeComConnector:
    def __init__(
        self,
        cfg: WeComHookConfig | None = None,
        *,
        bridge_factory: Callable[[], BridgeClient] | None = None,
        injector_run: Callable[[list[str], Callable[[str], None]], int] | None = None,
        find_pid: Callable[[str], int | None] | None = None,
        version_probe: Callable[[str], str] | None = None,
        launch: Callable[[str], None] | None = None,
        logger: Callable[[str], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        harden_fn: Callable[..., None] | None = None,
        login_probe: Callable[[BridgeClient], bool] | None = None,
        status_reader: Callable[[int, float], int | None] | None = None,
    ):
        self.cfg = cfg or WeComHookConfig()
        self._state = ConnState.IDLE
        self._lock = threading.Lock()
        self._log = logger or (lambda m: None)
        self._sleep = sleep
        self._bridge_factory = bridge_factory or self._make_bridge
        self._injector_run = injector_run or _default_injector_run
        self._find_pid = find_pid or winproc.find_process_pid
        self._version_probe = version_probe or winproc.file_version
        self._launch = launch or self._default_launch
        self._harden_fn = harden_fn or _default_harden
        # 登录态未逆向 → 默认认为已登录（软等待立即通过）；真机可注入探针。
        self._login_probe = login_probe or (lambda bridge: True)
        self._status_reader = status_reader or _default_status_reader
        self._token = self.cfg.token
        self._learned_port: int | None = None

    # ---- 状态 ----
    @property
    def state(self) -> ConnState:
        return self._state

    def _set(self, s: ConnState) -> None:
        self._state = s
        self._log(f"[wecom-connector] -> {s.value}")

    def _make_bridge(self) -> BridgeClient:
        port = getattr(self, "_learned_port", None) or self.cfg.bridge_port
        return BridgeClient(self.cfg.bridge_host, port, self._token, self.cfg.timeout_s)

    @staticmethod
    def _default_launch(exe: str) -> None:
        subprocess.Popen([exe])

    # ---- 主流程 ----
    def connect(self, pid: int | None = None, *, data_dir: str | None = None) -> WeComHookAdapter:
        """DETECTING → INJECTING → READY，返回就绪的 adapter；失败抛 WeComConnectError。

        `pid` 显式给定 → 跳过 `_detect()` 探测，直接用该 pid（多实例：由调用方逐个传入各
        WXWork pid）；注入后从 status 文件学 DLL 实际绑定端口（多实例各自不同端口，非静态
        `cfg.bridge_port`），学不到则回落 `cfg.bridge_port`。
        `pid=None` → 沿用旧行为：`_detect()` 探测第一个进程 + 静态 `cfg.bridge_port`（legacy 不回归）。
        """
        if not self._lock.acquire(blocking=False):
            raise WeComConnectError(self._state, "已有连接进行中（单飞互斥）")
        try:
            self._token = self.cfg.token or secrets.token_hex(16)
            target = pid if pid is not None else self._detect()
            self._version_gate()
            self._inject(target)
            self._learned_port = None
            if pid is not None:
                self._learned_port = self._status_reader(target, self.cfg.connect_timeout_s)
                if self._learned_port:
                    self._log(f"[wecom-connector] pid {target} 学到实际端口 {self._learned_port}")
                else:
                    self._log(f"[wecom-connector] pid {target} 未从 status 学到端口，回落 {self.cfg.bridge_port}")
            bridge = self._wait_ready()
            self._wait_login(bridge)
            self._harden()
            self._set(ConnState.CONNECTED)
            # 本地库历史反哺：把同一个 hook 桥的 /collog（col_hook 被动收割的 message.db 解密明文）
            # 接成 MsgDbReader，交给 adapter.harvest_history_texts()。关开关则不接（行为同接线前）。
            reader = (MsgDbReader(bridge.fetch_collog)
                      if self.cfg.enable_history_harvest else None)
            if self.cfg.persist_conversations:
                conv_path = (f"{data_dir}/wecom_conversations.json" if data_dir
                             else "wecom_conversations.json")
            else:
                conv_path = None
            # 进程内自查读全量历史（路线 B step3）。**self_id 不再依赖用户填**：配置留空则从本地库
            # 会话 id 自动反推（derive_self_id）。故 reader 只要开了 read_full_history 就建（不再 gate 在
            # self_id 上）；冷启动库句柄可能还没捕获→此刻推不出，交给 adapter 惰性重试。
            hist = (WeComHistoryReader(bridge.list_dbs, bridge.wecom_query, self_id=self.cfg.self_id)
                    if self.cfg.read_full_history else None)
            self_id = self.cfg.self_id
            if not self_id and hist is not None:
                try:
                    self_id = hist.derive_self_id()
                    if self_id:
                        hist.set_self_id(self_id)
                        self._log(f"[wecom-connector] 自动识别 self_id={self_id}")
                except Exception:
                    pass
            # 惰性解析器：冷启动此刻推不出时，adapter 在后续轮询/读会话时继续尝试直到拿到。
            resolver = (hist.derive_self_id if hist is not None else None)
            return WeComHookAdapter(bridge, self_id=self_id,
                                    poll_interval_s=self.cfg.poll_interval_s,
                                    msgdb_reader=reader, conv_state_path=conv_path,
                                    history_reader=hist, self_id_resolver=resolver)
        finally:
            self._lock.release()

    def connect_with_retry(self) -> WeComHookAdapter:
        """带指数退避 + 冷却上限的弹性连接。反复失败也不打爆机器（退避封顶 cooldown）。
        单飞互斥仍由 connect() 保证；末次失败抛出最后一个 WeComConnectError。"""
        last: WeComConnectError | None = None
        for attempt in range(1, self.cfg.reconnect_max_attempts + 1):
            try:
                return self.connect()
            except WeComConnectError as e:
                last = e
                if attempt >= self.cfg.reconnect_max_attempts:
                    break
                backoff = min(self.cfg.reconnect_cooldown_s,
                              self.cfg.reconnect_backoff_base_s * (2 ** (attempt - 1)))
                self._log(f"[wecom-connector] 第 {attempt} 次连接失败：{e.reason}；{backoff:.0f}s 后重试")
                self._sleep(backoff)
        assert last is not None
        raise last

    def _detect(self) -> int:
        self._set(ConnState.DETECTING)
        pid = self._find_pid(self.cfg.process_name)
        if pid is None and self.cfg.auto_launch and self.cfg.wework_exe_path:
            self._log(f"[wecom-connector] {self.cfg.process_name} 未运行，尝试启动")
            self._launch(self.cfg.wework_exe_path)
            self._sleep(1.5)
            pid = self._find_pid(self.cfg.process_name)
        if pid is None:
            self._fail(ConnState.DETECTING, f"未找到进程 {self.cfg.process_name}（未运行）")
        return pid

    def _version_gate(self) -> None:
        self._set(ConnState.VERSION_CHECK)
        if not self.cfg.wework_exe_path:
            self._log("[wecom-connector] 未配置 wework_exe_path，跳过版本闸")
            return
        actual = self._version_probe(self.cfg.wework_exe_path)
        ok, reason = offsets_mod.verify_version(self.cfg.required_version, actual, self.cfg.strict_version)
        self._log(f"[wecom-connector] 版本闸：{reason}")
        if not ok:
            self._set(ConnState.VERSION_MISMATCH)
            self._fail(ConnState.VERSION_MISMATCH, reason)

    def _pre_eject(self, pid: int) -> None:
        """注入前先卸一次残留 DLL（best-effort）。清不掉不致命，继续注入。"""
        if not self.cfg.eject_before_inject:
            return
        try:
            self._injector_run([self.cfg.injector_path, "--pid", str(pid), "--eject"], self._log)
            self._log("[wecom-connector] 注入前已尝试 eject 残留 DLL")
            if self.cfg.eject_settle_s > 0:
                self._sleep(self.cfg.eject_settle_s)
        except Exception as e:                          # noqa: BLE001 —— eject 失败不该拦住注入
            self._log(f"[wecom-connector] 注入前 eject 失败（忽略，继续注入）：{e}")

    def _inject(self, pid: int) -> None:
        self._set(ConnState.INJECTING)
        self._pre_eject(pid)
        args = [
            self.cfg.injector_path,
            "--pid", str(pid),
            "--dll", self.cfg.dll_path,
            "--port", str(self.cfg.bridge_port),
            "--token", self._token,
            "--wait",
        ]
        rc = self._injector_run(args, self._log)
        # rc==6=注入器 --wait 侧就绪超时：DLL 已加载、只是桥没在 15s 内起来。**不当致命失败**——
        # 交给随后的 Python 侧 _wait_ready 再多轮询 /health（connect_timeout_s），慢启动的桥能恢复；
        # 真起不来时 _wait_ready 会超时失败。其它非 0 退出码（找不到目标/位数/权限/加载失败）仍立即失败。
        if rc == 6:
            self._log("[wecom-connector] 注入器就绪超时(rc=6)：DLL 已加载，改由 Python 侧再等 /health")
            return
        if rc != 0:
            self._fail(ConnState.INJECTING, f"注入器退出码 {rc}（0=就绪 2=找不到目标 3=位数不符 4=权限 5=加载失败 6=就绪超时）")

    def _wait_ready(self) -> BridgeClient:
        # 注入器 --wait 已轮询过 /health；这里 Python 侧再权威确认一次（退出码0≠就绪）。
        self._set(ConnState.READY)
        bridge = self._bridge_factory()
        deadline = time.monotonic() + self.cfg.connect_timeout_s
        while time.monotonic() < deadline:
            if bridge.is_ready():
                self._log("[wecom-connector] 桥就绪 /health ok")
                return bridge
            self._sleep(self.cfg.health_interval_s)
        self._fail(ConnState.READY, "桥就绪超时（/health 一直不通）")

    def _wait_login(self, bridge: BridgeClient) -> None:
        """注入后软等待登录就绪。登录态尚未逆向 → 默认 login_probe 恒 True 立即通过；
        真机若注入了真实登录探针，则轮询到就绪或超时（超时=FAILED，不硬闯）。"""
        if not self.cfg.wait_login:
            return
        self._set(ConnState.WAITING_LOGIN)
        deadline = time.monotonic() + self.cfg.login_timeout_s
        while time.monotonic() < deadline:
            try:
                if self._login_probe(bridge):
                    self._log("[wecom-connector] 登录就绪")
                    return
            except Exception:                          # 探针异常不该崩，视作未就绪继续等
                pass
            self._sleep(self.cfg.login_interval_s)
        self._fail(ConnState.WAITING_LOGIN,
                   "等待登录超时（登录态未逆向→需注入 login_probe 或关 wait_login）")

    def _harden(self) -> None:
        """注入后硬化。**安全边界**：绝不卸载用户企微、绝不双标自动更新；
        默认保守跳过，仅在显式 harden_disable_autostart 时删企微开机自启。硬化失败绝不致命。"""
        self._set(ConnState.HARDENING)
        if not self.cfg.harden_disable_autostart:
            self._log("[wecom-connector] 硬化：保守默认跳过（不删自启/不碰企微更新/绝不卸载企微）")
            return
        try:
            self._harden_fn(self.cfg, self._log)
        except Exception as e:                          # noqa: BLE001 —— 硬化绝不能拖垮已建立的连接
            self._log(f"[wecom-connector] 硬化失败（忽略，不影响已连接）：{e}")

    def _fail(self, state: ConnState, reason: str):
        self._set(ConnState.FAILED)
        raise WeComConnectError(state, reason)

    def disconnect(self, pid: int | None = None) -> None:
        """请求卸载 hook（best-effort）。pid 已知走注入器 --eject，否则请桥自 /shutdown。"""
        try:
            if pid is not None:
                self._injector_run([self.cfg.injector_path, "--pid", str(pid), "--eject"], self._log)
            else:
                self._bridge_factory().shutdown()
        except Exception as e:      # noqa: BLE001 —— 卸载失败不该崩挂件
            self._log(f"[wecom-connector] disconnect 失败（忽略）：{e}")
        self._set(ConnState.STOPPED)
