# widget/instance_runtime.py
from __future__ import annotations
from widget.instances import InstanceConfig
from widget.state import RuntimeState

def build_instance(inst: InstanceConfig, cfg, bridge, state: RuntimeState,
                   *, adapter=None):
    """为一个已完成配置的个人抖音实例构造共享消息流水线。"""
    from widget.app import build_pipeline
    if adapter is None:
        raise ValueError("抖音实例必须提供渠道适配器")
    if getattr(inst, "channel_key", "").split("#", 1)[0] != "douyin":
        raise ValueError(f"不支持的渠道平台：{getattr(inst, 'channel_key', '')}")
    pipe = build_pipeline(cfg, adapter, bridge, state)
    state.set_self_wxid(inst.channel_key, adapter.self_wxid())
    return adapter, pipe
