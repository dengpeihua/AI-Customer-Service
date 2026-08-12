from __future__ import annotations
import copy
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

@dataclass
class InstanceState:
    channel_key: str
    platform: str
    account_id: str
    display_name: str
    pid: int
    status: str          # online|offline|injecting|no_login|parked|stopped
    mem_mb: int
    ai_enabled: bool
    last_error: str = ""
    backoff: float = 0.0
    port: int = 0        # M4：个人微信 hook 端口（企微/未知为 0）

def _default_sampler(pid: int) -> Optional[int]:
    try:
        import psutil
        return int(psutil.Process(pid).memory_info().rss / (1024 * 1024))
    except Exception:                                   # pid 没了/无权限
        return None

class InstanceSupervisor:
    def __init__(self, state, *, sampler: Optional[Callable[[int], Optional[int]]] = None,
                 reconnect_fns: Optional[dict] = None,
                 health_fns: Optional[dict] = None,
                 on_offline: Optional[Callable[[InstanceState], None]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 tick_interval_s: float = 5.0,
                 retry_interval_s: float = 3.0,
                 max_backoff_s: float = 60.0,
                 logger: Optional[Callable[[str], None]] = None):
        self._state = state
        self._sampler = sampler or _default_sampler
        self._reconnect_fns = reconnect_fns or {}
        # 「进程还活着」≠「这条渠道还健康」。platform -> (inst) -> bool：返回 False 就把这行
        # 打回 offline 重跑认领链。M4 H5 的用途：**原地换号**（同一个 Weixin.exe 退出登录、
        # 扫另一个号）—— pid 不变、端口不变、内存采得到，这行会永远 online，认领链再也不重跑，
        # 而 adapter 手里那条渠道已经指着别人的账号了。
        self._health_fns = health_fns or {}
        # 每次判定「这个号掉线了」都回调一次（不是只在状态翻转那一次）：掉线之后进程才死掉的
        # 情形同样要被处理。回调负责停掉它那条还在轮询的 adapter —— 掉线的号的端口随时可能被
        # 另一台微信接手，旧 adapter 只认端口，会把新主人的全部历史当成本号的入站消息。
        self._on_offline = on_offline
        self._clock = clock
        self._tick_interval_s = tick_interval_s
        # 「微信还没开」的固定重试周期。3.0 < tick_interval_s → 实际就是每个 tick 试一次
        # （5s），与 M3 之前那个 3s QTimer 同一量级；不需要为此把 tick 调快。
        self._retry_interval_s = retry_interval_s
        self._max_backoff_s = max_backoff_s
        self._log = logger or (lambda m: None)
        self._insts: dict[str, InstanceState] = {}
        self._next_retry: dict[str, float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def register(self, inst: InstanceState) -> None:
        with self._lock:
            self._insts[inst.channel_key] = inst

    def set_ai_enabled(self, channel_key: str, enabled: bool) -> None:
        self._state.set_ai_enabled(channel_key, enabled)
        with self._lock:
            if channel_key in self._insts:
                self._insts[channel_key].ai_enabled = enabled

    def tick(self) -> None:
        with self._lock:
            insts = list(self._insts.values())
        for inst in insts:
            try:
                self._tick_one(inst)
            except Exception as e:                      # noqa: BLE001 一实例失败不拖累其它
                self._log(f"[supervisor] {inst.channel_key} tick 失败（忽略）：{e}")

    def _go_offline(self, inst: InstanceState) -> None:
        """标 offline 并回调 on_offline（停掉那条可能已经在读别人微信的轮询）。"""
        inst.status = "offline"
        if self._on_offline is not None:
            try:
                self._on_offline(inst)
            except Exception as e:                  # noqa: BLE001 —— 停轮询失败不该拖垮采样
                self._log(f"[supervisor] {inst.channel_key} 掉线处理失败（忽略）：{e}")

    def _tick_one(self, inst: InstanceState) -> None:
        if inst.status == "stopped":
            return
        mem = self._sampler(inst.pid) if inst.pid else None
        if mem is None:
            if inst.pid and inst.status not in ("parked", "injecting", "no_login"):
                self._go_offline(inst)    # 有过真实 pid 却采不到=掉线；pid=0=未知 pid，不当掉线
        else:
            inst.mem_mb = mem
            if inst.status == "offline":
                inst.status = "online"
        # 内存采样之后再问健康：进程活着会把状态拉回 online，而「原地换号」恰恰是**进程活着**
        # 的那种坏。判据放在这里，本 tick 结束时它就是 offline，下面立刻重跑一次认领链。
        if inst.status == "online":
            fn = self._health_fns.get(inst.platform)
            if fn is not None:
                try:
                    healthy = bool(fn(inst))
                except Exception as e:              # noqa: BLE001 —— 探测失败绝不打下活渠道
                    healthy = True
                    self._log(f"[supervisor] {inst.channel_key} 健康检查失败（忽略）：{e}")
                if not healthy:
                    self._log(f"[supervisor] {inst.channel_key} 身份已漂移"
                              f"（进程还在，但登的不是认领时那个号）→ 重跑认领")
                    self._go_offline(inst)
        if inst.status in ("offline", "parked"):
            now = self._clock()
            if now >= self._next_retry.get(inst.channel_key, 0.0):
                fn = self._reconnect_fns.get(inst.platform)
                if fn is not None:
                    delay = self._retry_interval_s
                    try:
                        fn(inst)
                        # 「跑完了但没认回来」≠「一直在出错」：绝大多数情况就是客户还没把
                        # 微信打开（本项目最常见的顺序就是先起挂件后开微信）。这种情况必须
                        # 保持固定短周期重试 —— 指数退避会让「T 时刻开微信」的客户等最多 60s
                        # 才恢复自动回复（M3 之前是 3s 定时器，退避是回归）。认领本身只是枚举
                        # 进程 + 逐个问身份，每 tick 一次的开销可以接受。
                        inst.backoff = 0.0
                    except Exception as e:              # noqa: BLE001
                        # 真在报错（端口打不开/注入失败…）才退避：这类失败重试再快也没用，
                        # 而且往往是每次都要付出真实代价的操作，别把客户机烧了。
                        inst.last_error = str(e)
                        inst.backoff = min(self._max_backoff_s, (inst.backoff or 1.0) * 2)
                        delay = inst.backoff
                    self._next_retry[inst.channel_key] = now + delay

    def view(self) -> list:
        with self._lock:
            return [copy.copy(s) for s in self._insts.values()]

    def total_mem_mb(self) -> int:
        with self._lock:
            return sum(s.mem_mb for s in self._insts.values())

    def start(self) -> None:
        if self._thread:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self._tick_interval_s)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
