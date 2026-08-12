# widget/wecom/manager.py
"""WeComManager —— 多实例编排：每个 WXWork pid → connect → self_id 匹配 instances → 注册 hub。

设计要点（M2）：一台机器上可能开着多个企微客户端（多个 WXWork.exe 进程），每个进程对应
一个企微账号。本模块枚举所有 pid，逐个注入/连接拿到该账号的 self_id，用 self_id 去匹配
`instances.yaml` 里配置好的账号行（platform=="wecom"）。命中的账号才会被接入
ChannelHub（走该账号自己的 bridge/tenant，绝不混租户）；没在 instances.yaml 里配置的
pid 一律「park」——只记录、不接入，避免把陌生账号的消息误绑到任意已配置的租户上。
"""
from __future__ import annotations

from typing import Callable, Optional

from widget.instances import InstanceConfig
from widget.wecom import winproc


class WeComManager:
    """企微多实例编排：每个 WXWork pid → connect → self_id 匹配 instances → 注册 hub。"""

    def __init__(self, instances, cfg, bridges, state, hub, *,
                 process_name: str = "WXWork.exe",
                 find_pids: Optional[Callable[[str], list]] = None,
                 connector_factory: Optional[Callable] = None,
                 logger: Optional[Callable[[str], None]] = None,
                 wecom_cfg=None):
        self._wecom_insts = [i for i in instances if i.platform == "wecom"]
        self._cfg = cfg
        self._bridges = bridges
        self._state = state
        self._hub = hub
        self._process_name = process_name
        self._find_pids = find_pids or winproc.find_process_pids
        self._connector_factory = connector_factory or self._default_connector
        self._log = logger or (lambda m: None)
        self._wecom_cfg = wecom_cfg
        self.parked: dict[int, str] = {}
        self.adapters: dict[str, object] = {}
        self.pids: dict[str, int] = {}

    def _default_connector(self, cfg, logger, pid):
        from widget.wecom.connector import WeComConnector
        from widget.wecom.config import WeComHookConfig
        # 用调用方传入的真实 wecom 配置（含 wework_exe_path/injector_path/token/eject/harden 调优）
        # 而非裸默认配置——裸配置 wework_exe_path=="" 会让 WeComConnector._version_gate 直接跳过
        # 版本校验，等于对企微硬注不做版本闸（"绝不硬注写崩企微"红线）。
        return WeComConnector(self._wecom_cfg or WeComHookConfig(), logger=logger)

    def _match(self, self_id: str) -> Optional[InstanceConfig]:
        for i in self._wecom_insts:
            if i.self_id and i.self_id == self_id:
                return i
        return None

    def start(self) -> dict:
        from widget.instance_runtime import build_instance
        result: dict = {}
        for pid in self._find_pids(self._process_name):
            try:
                conn = self._connector_factory(self._cfg, self._log, pid)
                # data_dir 命中后才知道；先连出 self_id，再按匹配的 inst 决定（conv 落该账号目录）
                adapter = conn.connect(pid)
                self_id = adapter.self_wxid()
                inst = self._match(self_id)
                if inst is None:
                    self.parked[pid] = self_id
                    self._log(f"[wecom-manager] pid {pid} self_id={self_id} 未配置账号 → park")
                    continue
                # B5：adapter 的 channel 必须等于 hub 注册用的 channel_key，否则入站消息
                # 按 channel 路由时找不到 pipeline（ChannelHub.handle 全返回 "ignored"），
                # 多企微号一条消息都处理不了。connect() 拿不到这个值——channel_key 要靠
                # self_id 匹配 instances.yaml 才能确定，所以只能在这里补盖。
                adapter.channel = inst.channel_key
                # 多实例会话视图隔离：把该 pid 的 adapter 会话落盘路径重定位到本账号目录，
                # 避免 N 账号共用一个 wecom_conversations.json 串会话。load 在 start() 才发生，此刻重定位安全。
                if hasattr(adapter, "set_conv_state_path") and hasattr(adapter, "conv_state_path"):
                    if adapter.conv_state_path() is not None and inst.data_dir:
                        adapter.set_conv_state_path(f"{inst.data_dir}/wecom_conversations.json")
                bridge = self._bridges.get(inst.channel_key)
                _, pipe = build_instance(inst, self._cfg, bridge, self._state, adapter=adapter)
                self._hub.register(inst.channel_key, adapter, pipe)
                self.adapters[inst.channel_key] = adapter
                self.pids[inst.channel_key] = pid
                result[inst.channel_key] = ""
                self._log(f"[wecom-manager] pid {pid} → {inst.channel_key}（tenant {inst.tenant_id}）")
            except Exception as e:                       # noqa: BLE001 —— 单 pid 失败不拖垮其它
                self._log(f"[wecom-manager] pid {pid} 接入失败（忽略）：{e}")
                result[f"pid:{pid}"] = str(e)
        return result
