from __future__ import annotations
import dataclasses
from dataclasses import dataclass, field, asdict
from pathlib import Path
import yaml

@dataclass
class ScopeConfig:
    allow_private: bool = True
    private_mode: str = "selected"          # all | selected（仅接待会话白名单）
    allow_group: bool = False                # 当前个人抖音接入仅处理一对一私信
    group_trigger: str = "at_me"          # all | at_me | whitelist
    group_whitelist: list[str] = field(default_factory=list)
    group_blocklist: list[str] = field(default_factory=list)
    contact_blocklist: list[str] = field(default_factory=list)
    contact_allowlist: list[str] = field(default_factory=list)
    # 精确到抖音渠道的会话黑名单，值形如 "douyin#shop_a|user_id"。
    # GUI 的「接待此客户」开关写这里；命中后不调后端、不留后台记录、不弹通知。
    conversation_blocklist: list[str] = field(default_factory=list)
    conversation_allowlist: list[str] = field(default_factory=list)

@dataclass
class NotificationConfig:
    # 默认只提醒真正需要人工处理的消息；AI 已处理的消息安静留痕。
    handoff: bool = True
    auto_reply: bool = False
    # 即使有待人工也默认只发系统托盘通知，不抢占当前窗口焦点。
    bring_to_front: bool = False
    # 同一客户短时间连续发多条，只提醒一次，避免通知风暴。
    cooldown_s: float = 30.0

@dataclass
class BroadcastConfig:
    rate_per_min: int = 40
    delay_min_s: float = 3.0
    delay_max_s: float = 5.0
    batch_size: int = 50
    batch_rest_s: float = 60.0
    daily_limit: int = 200                # 群发独立日额度（不与被动回复共享计数）
    blacklist: list[str] = field(default_factory=list)

@dataclass
class WidgetConfig:
    backend_base_url: str = "http://127.0.0.1:8000"
    tenant_id: int = 0
    login: str = ""
    password: str = ""                    # 明文（宜改用 password_enc）
    password_enc: str = ""                # DPAPI 加密密码（优先，见 widget/secret.py）
    auto_send: bool = False
    poll_interval_s: float = 0.75
    send_delay_min_s: float = 1.5
    send_delay_max_s: float = 4.0
    rate_per_min: int = 8
    daily_limit: int = 200                # 0 = 不限
    handoff_reply: str = "帮您转接人工客服回复中，请稍等"
    scope: ScopeConfig = field(default_factory=ScopeConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    broadcast: BroadcastConfig = field(default_factory=BroadcastConfig)

def load_config(path: str | Path) -> WidgetConfig:
    p = Path(path)
    if not p.exists():
        return WidgetConfig()
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return WidgetConfig()
    scope_raw = raw.pop("scope", {}) or {}
    scope_fields = {f.name for f in dataclasses.fields(ScopeConfig)}
    scope = ScopeConfig(**{k: v for k, v in scope_raw.items() if k in scope_fields})
    notify_raw = raw.pop("notifications", {}) or {}
    notify_fields = {f.name for f in dataclasses.fields(NotificationConfig)}
    notifications = NotificationConfig(
        **{k: v for k, v in notify_raw.items() if k in notify_fields}
    )
    bc_raw = raw.pop("broadcast", {}) or {}
    bc_fields = {f.name for f in dataclasses.fields(BroadcastConfig)}
    broadcast = BroadcastConfig(**{k: v for k, v in bc_raw.items() if k in bc_fields})
    cfg_fields = {f.name for f in dataclasses.fields(WidgetConfig)} - {
        "scope", "notifications", "broadcast"
    }
    return WidgetConfig(scope=scope, notifications=notifications, broadcast=broadcast,
                        **{k: v for k, v in raw.items() if k in cfg_fields})

def save_config(cfg: WidgetConfig, path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    d = asdict(cfg)
    p.write_text(
        yaml.safe_dump(d, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
