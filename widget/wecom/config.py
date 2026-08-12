from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# 本渠道相关的原生产物默认布局（native/build/out/）。
_NATIVE = Path(__file__).resolve().parents[2] / "native"
_DEFAULT_OUT = _NATIVE / "build" / "out"


@dataclass
class WeComHookConfig:
    """企微自研 hook 渠道配置。"""
    # --- 桥 ---
    bridge_host: str = "127.0.0.1"          # 只连本机，绝不对外
    bridge_port: int = 8752
    token: str = ""                          # 每次连接随机生成更佳（连接器可覆盖）
    timeout_s: float = 3.0
    connect_timeout_s: float = 20.0          # 注入→/health 就绪的最长等待（对齐 waitForInjection）
    health_interval_s: float = 0.3
    poll_interval_s: float = 1.0             # 入站消息轮询间隔（adapter 走桥 /messages）

    # --- 编排硬化（F3）---
    wait_login: bool = False                 # 注入后是否软等待登录（登录态未逆向→默认关，需 login_probe 注入）
    login_timeout_s: float = 300.0           # 软等待登录超时（对齐 WeiClaw 扫码 5 分钟）
    login_interval_s: float = 2.0
    harden_disable_autostart: bool = False   # 注入后是否删企微开机自启（保守默认关；**绝不卸载用户企微/不双标自动更新**）
    reconnect_max_attempts: int = 5          # connect_with_retry 最多尝试次数
    reconnect_backoff_base_s: float = 2.0    # 指数退避基数（2,4,8… 上限 cooldown）
    reconnect_cooldown_s: float = 60.0       # 退避上限 + 失败冷却，防 kill/注入循环打爆机器

    # --- 本地库历史反哺（col_hook /collog）---
    enable_history_harvest: bool = True      # 接 col_hook 被动收割 → MsgDbReader 反哺连接前历史+人工手打；
                                             # 关则 adapter.harvest_history_texts() 恒空（行为同接线前）
    auto_history_sync: bool = False          # 默认关：聊天历史只能由客服选择会话后手动总结入库；
                                             # 明确开启才允许守护线程周期收割→蒸馏入库
    history_sync_interval_s: float = 20.0    # 收割轮询间隔
    history_flush_interval_s: float = 300.0  # 反哺节流：攒够 min_batch 且过此间隔才打一次 LLM 蒸馏端点
    history_min_batch: int = 6               # 攒够几条才反哺（防零星噪声频繁打 LLM）
    read_full_history: bool = True           # 进程内自查读企微本地库【全量】历史（路线 B step3）；
                                             # 需 self_id 才能拼 1:1 会话 id。关则回落"连接后实时视图"
    # --- 会话视图持久化 ---
    persist_conversations: bool = True       # 把实时 recv/send 会话视图落盘，重启后 GUI 仍见历史记录

    # --- 目标企微 ---
    process_name: str = "WXWork.exe"
    required_version: str = "5.0.3.6005"     # 强绑定版本；不符拒绝启动（见 offsets）
    strict_version: bool = True              # M2 起默认严格：版本读不到/不符即拒（已真机核对 5.0.3.6005）
    wework_exe_path: str = ""                # 已知则可校验版本 / 未运行时启动
    auto_launch: bool = False                # 目标未运行时是否由连接器启动

    # 注入前先 eject 一次残留 DLL：若上次运行/企微未重启留下旧的注入 DLL 仍占着 8752，
    # 直接重注入会命中 LoadLibrary 既有句柄、DllMain 不再跑 → 新桥不起、/health 恒超时
    # （真机日志"injected HMODULE ok 但 :8752 TIMEOUT→退出码6→企微渠道未接入"的根因之一）。
    # 先 eject 清干净再注入，保证 DllMain 重新起桥。best-effort，清不掉也继续。
    eject_before_inject: bool = True
    eject_settle_s: float = 1.5              # eject 后等企微卸载完再注入

    # --- 原生产物 ---
    injector_path: str = str(_DEFAULT_OUT / "wecom_injector.exe")
    dll_path: str = str(_DEFAULT_OUT / "wecom_hook.dll")
    offsets_path: str = str(_NATIVE / "offsets" / "wework-5.0.3.6005.yaml")

    self_id: str = ""                        # 本企微账号 id（骨架期无法自动取，手填）

    @staticmethod
    def load(path: str | Path) -> "WeComHookConfig":
        p = Path(path)
        if not p.exists():
            return WeComHookConfig()
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            return WeComHookConfig()
        names = {f.name for f in dataclasses.fields(WeComHookConfig)}
        return WeComHookConfig(**{k: v for k, v in raw.items() if k in names})
