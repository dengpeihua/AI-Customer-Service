"""无界面个人抖音 AI 客服：扫描私信，生成回复或转待人工。"""
from __future__ import annotations

import sys
import time

from widget.app import build_pipeline
from widget.bridge_pool import BridgePool
from widget.channel_hub import ChannelHub
from widget.config import WidgetConfig, load_config
from widget.instances import load_instances
from widget.state import RuntimeState
from widget.douyin.adapter import DouyinAdapter


def handle_message(hub: ChannelHub, message, *, log=print) -> str:
    """Route one event and return the outcome required by persistent reconciliation."""
    action = hub.handle(message)
    log(
        f"[收] {message['channel']} {message['sender_id']}: "
        f"{message['text']!r} -> {action}",
        flush=True,
    )
    return action


def main(config_path: str = "widget_config.yaml") -> None:
    from widget.single_instance import acquire

    if not acquire("AI-customer-service-widget"):
        print("抖音 AI 客服已在运行（单实例锁）。")
        return
    try:
        cfg = load_config(config_path)
    except Exception:
        cfg = WidgetConfig()
    state = RuntimeState(cfg, config_path=config_path)
    accounts = [item for item in load_instances() if item.enabled]
    if not accounts:
        print("[启动] 未配置 douyin_accounts.yaml，退出。", flush=True)
        return

    pool = BridgePool(cfg, log=lambda message: print(message, flush=True))
    hub = ChannelHub()
    for account in accounts:
        try:
            bridge = pool.for_instance(account)
            bridge.login()
            adapter = DouyinAdapter(account)
            hub.register(account.channel_key, adapter, build_pipeline(cfg, adapter, bridge, state))
        except Exception as exc:  # noqa: BLE001 - one bad account must not stop the others
            print(f"[启动] {account.channel_key} 后端登录失败，已跳过：{exc}", flush=True)
    if not hub.channels():
        print("[启动] 没有账号通过后端登录，退出。", flush=True)
        return

    def on_message(message) -> str:
        return handle_message(hub, message)

    started = hub.start_all(on_message)
    for channel, error in started.items():
        print(f"[启动] {channel}: {'OK' if not error else error}", flush=True)
    if not any(not error for error in started.values()):
        print("[启动] 所有抖音账号均连接失败，退出。", flush=True)
        hub.stop_all()
        return
    print("[启动] 正在扫描抖音个人私信；Ctrl+C 退出。", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[退出] 停止抖音私信扫描", flush=True)
    finally:
        hub.stop_all()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "widget_config.yaml")
