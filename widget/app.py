from __future__ import annotations

import threading
import time
from pathlib import Path

from widget.bridge_pool import BridgePool
from widget.config import WidgetConfig, load_config
from widget.inbound import InboundFilter
from widget.models import InboundMsg
from widget.pipeline import Pipeline
from widget.sender import RateLimiter, Sender
from widget.state import RuntimeState


def build_pipeline(cfg: WidgetConfig, adapter, bridge, state: RuntimeState) -> Pipeline:
    limiter = RateLimiter(cfg.rate_per_min, cfg.daily_limit)
    sender = Sender(adapter, cfg, limiter)
    self_id = adapter.self_wxid()
    state.set_self_wxid(adapter.channel, self_id)
    state.limiter = limiter

    def on_pending(msg, result) -> None:
        created = state.add_pending(msg, result)
        action = result.get("record_action", "handoff")
        state.record_inbound(msg, action, count_received=created)
        if action == "handoff_notified" and cfg.handoff_reply:
            state.publish_outbound(msg, cfg.handoff_reply, "ai")

    def on_auto(msg, result) -> None:
        recovered = state.remove_pending_for_message(msg)
        if result.get("reconciliation_only"):
            return
        state.record_inbound(msg, "auto_reply", count_received=not recovered)
        reply_text = str(result.get("reply_text") or "")
        if reply_text:
            state.publish_outbound(msg, reply_text, "ai")

    return Pipeline(
        cfg,
        InboundFilter(),
        bridge,
        sender,
        self_id,
        on_pending=on_pending,
        on_auto=on_auto,
        is_ai_enabled=lambda: state.is_ai_enabled(adapter.channel),
    )


def load_instances_or_degrade(cfg=None, *, load=None, legacy=None, log=print) -> list:
    from widget.instances import legacy_instances, load_instances

    try:
        accounts = (load or load_instances)()
        if accounts:
            return accounts
        log("[启动] 未找到抖音账号配置，工作台以诊断模式启动")
    except Exception as exc:
        log(f"[启动] douyin_accounts.yaml 无效，工作台以诊断模式启动：{exc}")
    return (legacy or legacy_instances)(wc=cfg)


def side_panel_bridge(pool, instances: dict, channel_key: str, cfg):
    item = instances.get(channel_key)
    return pool.for_instance(item) if item is not None else pool.for_config(cfg)


def run(
    config_path: str = "widget_config.yaml",
    *,
    adapter=None,
    on_ownership_acquired=None,
) -> bool:
    from PySide6.QtCore import QObject, QTimer, Signal
    from PySide6.QtWidgets import QApplication

    from widget.channel_hub import ChannelHub
    from widget.handoff import HandoffController
    from widget.instance_supervisor import InstanceState, InstanceSupervisor
    from widget.instances import load_instances
    from widget.notifications import NotificationGate
    from widget.single_instance import acquire
    from widget.ui.app_icon import apply_windows_window_icon, load_app_icon, set_windows_app_user_model_id
    from widget.ui.pages.instances_page import InstancesConsolePage
    from widget.ui.pages.douyin_page import DouyinPage
    from widget.ui.startup_splash import StartupSplash
    from widget.ui.theme import apply_theme
    from widget.ui.tray import Tray
    from widget.ui.window_activation import show_front
    from widget.ui.workbench import WorkbenchWindow
    from widget.douyin.adapter import DouyinAdapter
    from widget.douyin.contact_source import DouyinContactSource

    if not acquire("AI-customer-service-widget"):
        print("[启动] 抖音 AI 客服工作台已在运行")
        return False
    if on_ownership_acquired is not None:
        on_ownership_acquired()

    try:
        cfg = load_config(config_path)
    except Exception:
        cfg = WidgetConfig()
    state = RuntimeState(cfg, config_path=config_path)

    set_windows_app_user_model_id()
    qt_app = QApplication.instance() or QApplication([])
    qt_app.setApplicationName("抖音 AI 客服")
    qt_app.setApplicationDisplayName("抖音 AI 客服")
    app_icon = load_app_icon()
    if not app_icon.isNull():
        qt_app.setWindowIcon(app_icon)
    qt_app.setQuitOnLastWindowClosed(False)
    apply_theme(qt_app)
    startup = StartupSplash(app_icon)
    startup.show()
    startup.set_stage("正在连接抖音账号与 AI 服务…")

    class Signals(QObject):
        refresh = Signal()
        notification = Signal(str, str)
        open_handoff = Signal()
        conversation_event = Signal(object)

    signals = Signals()
    state.set_conversation_listener(signals.conversation_event.emit)
    pool = BridgePool(cfg, log=print)
    accounts = [item for item in load_instances_or_degrade(cfg, load=load_instances, log=print)
                if item.enabled]
    hub = ChannelHub()
    adapters: list = []
    bridges: dict[str, object] = {}

    if adapter is not None:
        bridge = pool.for_config(cfg)
        hub.register(adapter.channel, adapter, build_pipeline(cfg, adapter, bridge, state))
        adapters.append(adapter)
        bridges[adapter.channel] = bridge
    else:
        for account in accounts:
            channel_adapter = DouyinAdapter(account)
            channel_bridge = pool.for_instance(account)
            hub.register(
                account.channel_key,
                channel_adapter,
                build_pipeline(cfg, channel_adapter, channel_bridge, state),
            )
            adapters.append(channel_adapter)
            bridges[account.channel_key] = channel_bridge

    if not adapters:
        raise RuntimeError("没有可用的抖音账号；请复制并填写 douyin_accounts.example.yaml")
    primary = adapters[0]
    primary_channel = primary.channel
    account_by_channel = {item.channel_key: item for item in accounts}
    primary_bridge = bridges[primary_channel]
    contact_source = DouyinContactSource(primary)

    supervisor = InstanceSupervisor(state)
    for channel_adapter in adapters:
        item = account_by_channel.get(channel_adapter.channel)
        supervisor.register(InstanceState(
            channel_key=channel_adapter.channel,
            platform="douyin",
            account_id=item.account_id if item else channel_adapter.channel,
            display_name=item.display_name if item else "抖音",
            pid=0,
            status="connecting",
            mem_mb=0,
            ai_enabled=True,
        ))
    labels = {
        item.channel_key: item.display_name for item in accounts
    } or {primary_channel: "抖音"}
    controller = HandoffController(hub, state)

    def new_instance(_platform: str) -> None:
        print("[启动] 请编辑 douyin_accounts.yaml 添加账号后重启工作台")

    console = InstancesConsolePage(
        supervisor,
        on_open_chat=lambda channel: workbench.show_history_for_channel(channel),
        on_new_instance=new_instance,
    )
    channels = [(channel, labels.get(channel, channel)) for channel in hub.channels()]
    workbench = WorkbenchWindow(
        state,
        controller,
        primary_bridge,
        adapter=primary,
        history_source=contact_source,
        history_channels=channels,
        history_adapter_for=hub.adapter,
        memory_bridge_for=lambda channel: bridges.get(channel, primary_bridge),
        history_default_channel=primary_channel,
        instances_page=console,
        douyin_page=DouyinPage(adapters),
        labels=labels,
        app_icon=app_icon,
    )
    signals.refresh.connect(workbench.refresh)
    signals.conversation_event.connect(workbench.history_page.apply_live_event)
    signals.conversation_event.connect(workbench.memory_conversation_page.apply_live_event)
    tray = Tray(
        state,
        on_open=lambda: show_front(workbench),
        on_quit=qt_app.quit,
        on_handoff=workbench.show_handoff,
        app_icon=app_icon,
    )
    tray.show()
    signals.notification.connect(tray.notify)
    signals.open_handoff.connect(workbench.show_handoff)
    notification_gate = NotificationGate(cfg.notifications)
    show_front(workbench)
    startup.finish(workbench)
    apply_windows_window_icon(workbench, app_icon)
    print("[启动] 工作台界面已显示；抖音浏览器与 AI 服务将在后台完成连接")

    def on_message(message: InboundMsg) -> str:
        signals.conversation_event.emit({
            "event_id": f"in:{message.get('channel', '')}:{message.get('msg_id', '')}",
            "direction": "inbound",
            "channel": message.get("channel", ""),
            "contact_id": message.get("contact_id", ""),
            "sender_id": message.get("sender_id", ""),
            "sender_name": message.get("sender_name", ""),
            "sender_avatar": message.get("sender_avatar", ""),
            "text": message.get("text", ""),
            "kind": message.get("kind", "text"),
            "media_url": message.get("media_url", ""),
            "title": message.get("title", ""),
            "description": message.get("description", ""),
            "url": message.get("url", ""),
            "display_time": message.get("display_time", ""),
            "video_url": message.get("video_url", ""),
            "quote_sender": message.get("quote_sender", ""),
            "quote_text": message.get("quote_text", ""),
            "source_type": message.get("source_type", ""),
            "timestamp": int(message.get("timestamp") or 0),
            "is_group": False,
            "provenance": "customer",
        })
        action = hub.handle(message)
        if action == "ignored":
            return action
        channel = message.get("channel", "")
        if notification_gate.should_notify(action, channel, message["contact_id"]):
            if action in ("handoff", "error", "delivery_uncertain", "delivery_waiting"):
                signals.notification.emit("有新的抖音私信待人工", message["text"][:20])
                if cfg.notifications.bring_to_front:
                    signals.open_handoff.emit()
            else:
                signals.notification.emit("AI 已处理抖音私信", message["text"][:20])
        signals.refresh.emit()
        return action

    workbench.history_page.live_tick()
    started = hub.start_all(on_message)
    for channel, error in started.items():
        if error:
            print(f"[启动] 抖音渠道 {channel} 未启动：{error}")

    def refresh_channel_health() -> None:
        errors: list[str] = []
        healthy = 0
        for channel_adapter in adapters:
            status_fn = getattr(channel_adapter, "status_snapshot", None)
            status = status_fn() if callable(status_fn) else {}
            ok = bool(status.get("healthy", getattr(channel_adapter, "healthy", True)))
            error = str(
                status.get("last_error", getattr(channel_adapter, "last_error", "")) or ""
            )
            healthy += int(ok)
            if error:
                errors.append(f"{channel_adapter.channel}: {error}")
            supervisor.set_status(
                channel_adapter.channel, "online" if ok else "offline", error,
            )
        state.hook_ok = healthy > 0
        state.hook_error = "；".join(errors)
        tray.set_health(
            ok=state.hook_ok and state.backend_ok,
            degraded=not (state.hook_ok and state.backend_ok),
        )

    channel_health_timer = QTimer()
    channel_health_timer.timeout.connect(refresh_channel_health)
    channel_health_timer.start(5_000)
    refresh_channel_health()

    follow_timer = QTimer()
    follow_timer.timeout.connect(
        lambda: workbench.history_page.live_tick()
        if workbench.current_page_name() == "history" else None
    )
    follow_timer.start(3_000)

    def login_backend() -> None:
        last_error = "AI 服务正在启动"
        for _ in range(80):
            if qt_app.closingDown():
                return
            all_ready = True
            for item_bridge in pool.all():
                try:
                    item_bridge.login()
                except Exception as exc:  # noqa: BLE001
                    all_ready = False
                    last_error = str(exc)
                    if "401" in last_error or "403" in last_error:
                        break
            if all_ready and pool.all():
                state.backend_ok = True
                state.backend_error = ""
                signals.refresh.emit()
                return
            time.sleep(0.5)
        state.backend_ok = False
        state.backend_error = last_error
        signals.refresh.emit()

    threading.Thread(target=login_backend, name="backend-login", daemon=True).start()
    supervisor.start()
    workbench.refresh()
    qt_app.exec()
    supervisor.stop()
    hub.stop_all()
    return True
