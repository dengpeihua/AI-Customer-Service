"""无界面版挂件（收→答→发闭环，不依赖 PySide6/GUI）。

用于：GUI 启动有问题时、服务器/后台运行、或端到端测试。
逻辑与 widget.app.run() 相同，只是去掉 Qt 托盘/状态窗，改为纯轮询循环 + 终端日志。

用法：
    .venv\\Scripts\\python.exe -u run_widget_headless.py [widget_config.yaml]
Ctrl+C 退出。
"""
from __future__ import annotations

import sys
import time

from widget.app import build_pipeline
from widget.adapters.wechat_hook import WeChatHookAdapter
from widget.bridge import BridgeError
from widget.bridge_pool import BridgePool
from widget.config import WidgetConfig, load_config
from widget.state import RuntimeState


def main(config_path: str = "widget_config.yaml") -> None:
    from widget.single_instance import acquire
    if not acquire("AI-customer-service-widget"):
        print("挂件已在运行（单实例锁），本次不重复启动。")
        return
    try:
        cfg = load_config(config_path)
    except Exception:
        cfg = WidgetConfig()
    state = RuntimeState(cfg, config_path=config_path)

    # ★M5★ 走 BridgePool（与 app.py 一致）：headless 是单实例单租户 → for_config 按去重键
    # (backend,tenant,login) 返回的就是与旧版 `Bridge(cfg)` 逐字节等价的那一个 Bridge（零回归）。
    pool = BridgePool(cfg, log=lambda m: print(m, flush=True))
    bridge = pool.for_config(cfg)
    try:
        bridge.login()
        state.backend_ok = True
        print(f"[启动] 后端登录成功 tenant={cfg.tenant_id} login={cfg.login}", flush=True)
    except BridgeError as e:
        state.backend_ok = False
        print(f"[启动] [!] 后端登录失败({e})：消息将全部转待人工，不会自动回复", flush=True)

    adapter = WeChatHookAdapter(cfg, state_path="widget_cursor.json")
    try:
        pipe = build_pipeline(cfg, adapter, bridge, state)   # 内部会 adapter.self_wxid()
        state.hook_ok = True
        print(f"[启动] hook 就绪 self_wxid={state.self_wxid} | 冷启动基线已设，只处理新消息", flush=True)
    except Exception as e:
        print(f"[启动] [X] hook 未就绪({e})：微信没开/未注入/未 patch g_IsLogin，退出", flush=True)
        return

    def on_msg(m):
        action = pipe.handle(m)    # auto_reply | handoff | ignored | error
        label = {"auto_reply": "自动回复[OK]", "handoff": "转待人工", "ignored": "已忽略",
                 "error": "后端出错→待人工"}.get(action, action)
        print(f"[收] {m['sender_id']} @{m['contact_id']}: {m['text']!r}  ->  {label}", flush=True)

    adapter.start(on_message=on_msg)
    print(f"[启动] 开始轮询微信（每 {cfg.poll_interval_s}s）。用另一台微信给本号发【营业时间】或【配送】；Ctrl+C 退出。", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[退出] 停止轮询", flush=True)
    finally:
        adapter.stop()


if __name__ == "__main__":
    import os
    try:
        main(sys.argv[1] if len(sys.argv) > 1 else "widget_config.yaml")
    finally:
        os._exit(0)   # 退出时兜底强杀残留轮询/网络线程，不让进程滞留后台
