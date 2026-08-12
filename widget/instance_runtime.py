# widget/instance_runtime.py
from __future__ import annotations
from pathlib import Path
from widget.instances import InstanceConfig
from widget.state import RuntimeState

def build_instance(inst: InstanceConfig, cfg, bridge, state: RuntimeState,
                   *, adapter=None, adapter_factory=None,
                   verified_pid: int = 0, legacy: bool = False):
    """造一个实例的 adapter+pipeline。

    `verified_pid`：**已经过身份认领**的微信 pid（只有 WeChatManager 给得出）。adapter 拿它
    去打 g_IsLogin 补丁；给不出就意味着「这个端口上是谁还没验证」——adapter 于是一个字节都不写。
    `legacy`：无 instances.yaml 的现网单实例路径，逐字节沿用旧的按端口打补丁行为（规则 4）。
    两个都不给 = 默认 fail closed（不写内存），因为写错进程会写崩客户的微信。
    """
    from widget.app import build_pipeline
    if adapter is None:
        if inst.platform == "wechat":
            if adapter_factory is None:
                from widget.adapters.wechat_hook import WeChatHookAdapter
                adapter_factory = WeChatHookAdapter
            Path(inst.data_dir).mkdir(parents=True, exist_ok=True)
            adapter = adapter_factory(
                cfg, state_path=f"{inst.data_dir}/widget_cursor.json",
                self_wxid_override=inst.self_wxid, channel_key=inst.channel_key,
                base_url=inst.resolve_hook_base_url(cfg.hook_base_url),
                verified_pid=int(verified_pid or 0), legacy_patch=bool(legacy),
                # `claimed_wxid`：这条渠道**认领的是哪个号**。adapter 每次收发前拿它问一次
                # 「这个 pid 此刻还开着这个号的数据目录吗」——原地退出登录换个号扫码，pid 与
                # 端口全不变，只有这个信号会变（M4 H5）。
                claimed_wxid=inst.self_wxid)
        else:
            raise ValueError("wecom 实例经 widget/wecom/manager.py 构建（M2）")
    pipe = build_pipeline(cfg, adapter, bridge, state)
    state.set_self_wxid(inst.channel_key, adapter.self_wxid())
    return adapter, pipe
