from __future__ import annotations

from pathlib import Path

from app.channels.douyin.config import (
    DouyinAccount as InstanceConfig,
    DouyinConfigError as InstanceConfigError,
    load_douyin_accounts,
)


def load_instances(
    instances_path: str = "douyin_accounts.yaml",
    *_args,
    **_kwargs,
) -> list[InstanceConfig]:
    """加载个人抖音多账号配置。"""
    return load_douyin_accounts(instances_path)


def legacy_instances(
    _widget_cfg_path: str = "widget_config.yaml",
    _channel_cfg_path: str = "",
    wc=None,
) -> list[InstanceConfig]:
    """没有账号文件时构造不可误发的本地占位账号，让工作台仍能显示诊断。"""
    if wc is None:
        from widget.config import load_config

        wc = load_config(_widget_cfg_path)
    return [InstanceConfig(
        account_id="default",
        display_name="抖音（待配置）",
        tenant_id=max(1, int(getattr(wc, "tenant_id", 0) or 0)),
        login=str(getattr(wc, "login", "") or ""),
        password=str(getattr(wc, "password", "") or ""),
        password_enc=str(getattr(wc, "password_enc", "") or ""),
        enabled=True,
        data_dir=str(Path("data") / "douyin" / "default"),
    )]
