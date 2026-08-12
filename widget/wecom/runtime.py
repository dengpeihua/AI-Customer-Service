"""企微自研 hook 运行时组装 —— 把注入就绪的 WeComHookAdapter 接进挂件产品管线。

个人微信走 widget/app.py::create_wechat_runtime；企微自研 hook 走这里：
  WeComConnector.connect()（注入 + 就绪）→ WeComHookAdapter → 复用 build_pipeline。

产出的 (adapter, pipe) 与个人微信同构，可直接喂给 app.run 的 on_msg 闭环；
发送经 Sender 同样受 WidgetConfig.auto_send / 频控 / scope 保护（安全开关一致，
自动发默认仍 auto_send=False，要真机受控放开才会发）。收→answer()→headless 回 的闭环
即：adapter 轮询 /messages 得客户消息 → pipe.handle → bridge.chat 调后端 answer()
→ Sender.deliver → adapter.send_message → 桥 /hsend（headless 数据层，不抢前台、可并发）。

connect_fn 可注入（测试用 fake，免真注入 / 真企微）。
"""
from __future__ import annotations

from typing import Callable

from widget.config import WidgetConfig
from widget.state import RuntimeState
from widget.wecom.adapter import WeComHookAdapter
from widget.wecom.config import WeComHookConfig
from widget.wecom.connector import WeComConnector


def create_wecom_runtime(
    cfg: WidgetConfig,
    bridge,
    state: RuntimeState,
    wecom_cfg: WeComHookConfig | None = None,
    *,
    connect_fn: Callable[[], WeComHookAdapter] | None = None,
    logger: Callable[[str], None] | None = None,
):
    """组装企微自研 hook 运行时，返回 (adapter, pipe)。

    connect_fn 缺省时用 WeComConnector(wecom_cfg).connect() 真注入并返回就绪 adapter；
    失败会抛 WeComConnectError（由调用方降级为「企微未连接」，绝不崩挂件）。
    """
    # 延迟导入避免任何潜在环依赖（app.py 顶层不引 widget.wecom.*）。
    from widget.app import build_pipeline

    if connect_fn is None:
        conn = WeComConnector(wecom_cfg or WeComHookConfig(), logger=logger)
        adapter = conn.connect()
    else:
        adapter = connect_fn()

    pipe = build_pipeline(cfg, adapter, bridge, state)   # 内部 adapter.self_wxid()→state.self_wxid
    state.hook_ok = True
    state.hook_error = ""
    return adapter, pipe
