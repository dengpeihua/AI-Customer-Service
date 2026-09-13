from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml


_ACCOUNT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_BROWSER_CHANNELS = {"", "chrome", "msedge"}


class DouyinConfigError(ValueError):
    """Raised when a Douyin account configuration is unsafe or incomplete."""


@dataclass(frozen=True)
class DouyinAccount:
    account_id: str
    display_name: str
    tenant_id: int
    login: str
    password_enc: str = ""
    password: str = ""
    enabled: bool = True
    expected_identity_fingerprint: str = ""
    messages_url: str = "https://www.douyin.com/"
    browser_channel: str = "chrome"
    profile_dir: str = ""
    headless: bool = False
    poll_interval_s: float = 3.0
    navigation_timeout_s: float = 30.0
    process_existing_messages: bool = False
    send_enabled: bool = False
    data_dir: str = ""

    @property
    def channel_key(self) -> str:
        return f"douyin#{self.account_id}"

    @property
    def resolved_data_dir(self) -> Path:
        return Path(self.data_dir or f"data/douyin/{self.account_id}")

    @property
    def resolved_profile_dir(self) -> Path:
        if self.profile_dir:
            return Path(self.profile_dir)
        local_app_data = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
        return local_app_data / "AI-Customer-Service" / "douyin" / self.account_id / "chrome-profile"

    def public_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "display_name": self.display_name,
            "tenant_id": self.tenant_id,
            "enabled": self.enabled,
            "channel_key": self.channel_key,
            "expected_identity_configured": bool(self.expected_identity_fingerprint),
            "profile_dir": str(self.resolved_profile_dir),
            "headless": self.headless,
            "poll_interval_s": self.poll_interval_s,
            "process_existing_messages": self.process_existing_messages,
            "send_enabled": self.send_enabled,
        }


def _build_account(row: dict) -> DouyinAccount:
    allowed = set(DouyinAccount.__dataclass_fields__)
    unknown = sorted(set(row) - allowed)
    if unknown:
        raise DouyinConfigError(f"抖音账号配置包含未知字段：{', '.join(unknown)}")
    required = {"account_id", "display_name", "tenant_id", "login"}
    missing = sorted(key for key in required if key not in row)
    if missing:
        raise DouyinConfigError(f"抖音账号配置缺少字段：{', '.join(missing)}")
    string_fields = (
        "account_id", "display_name", "login", "password_enc", "password",
        "expected_identity_fingerprint", "messages_url", "browser_channel", "profile_dir",
        "data_dir",
    )
    for key in string_fields:
        if key in row and not isinstance(row[key], str):
            raise DouyinConfigError(f"{key} 必须是字符串")
    for key in ("enabled", "headless", "process_existing_messages", "send_enabled"):
        if key in row and not isinstance(row[key], bool):
            raise DouyinConfigError(f"{key} 必须是 YAML 布尔值 true 或 false")
    if not isinstance(row.get("tenant_id"), int) or isinstance(row.get("tenant_id"), bool):
        raise DouyinConfigError("tenant_id 必须是整数")
    for key in ("poll_interval_s", "navigation_timeout_s"):
        if key in row and (
            not isinstance(row[key], (int, float)) or isinstance(row[key], bool)
        ):
            raise DouyinConfigError(f"{key} 必须是数值")
    try:
        account = DouyinAccount(**row)
    except TypeError as exc:
        raise DouyinConfigError(f"抖音账号配置无效：{exc}") from exc
    if not _ACCOUNT_ID.fullmatch(account.account_id):
        raise DouyinConfigError(f"account_id 非法：{account.account_id!r}")
    if not account.display_name.strip():
        raise DouyinConfigError(f"账号 {account.account_id} 缺少 display_name")
    if account.tenant_id <= 0:
        raise DouyinConfigError(f"账号 {account.account_id} 的 tenant_id 必须为正整数")
    if not account.login:
        raise DouyinConfigError(f"账号 {account.account_id} 缺少后端 login")
    if account.password:
        raise DouyinConfigError(
            f"账号 {account.account_id} 不允许明文 password；请使用 widget.secret 生成 password_enc"
        )
    parsed = urlparse(account.messages_url)
    if parsed.scheme != "https" or parsed.hostname not in {"douyin.com", "www.douyin.com"}:
        raise DouyinConfigError(f"账号 {account.account_id} 的 messages_url 必须是抖音 HTTPS 地址")
    if account.browser_channel not in _BROWSER_CHANNELS:
        raise DouyinConfigError(
            f"账号 {account.account_id} 的 browser_channel 只允许 chrome、msedge 或空值"
        )
    if not 2.0 <= float(account.poll_interval_s) <= 60.0:
        raise DouyinConfigError(f"账号 {account.account_id} 的 poll_interval_s 超出 2..60")
    if not 5.0 <= float(account.navigation_timeout_s) <= 120.0:
        raise DouyinConfigError(f"账号 {account.account_id} 的 navigation_timeout_s 超出 5..120")
    if account.expected_identity_fingerprint and not re.fullmatch(
        r"[a-f0-9]{16}", account.expected_identity_fingerprint
    ):
        raise DouyinConfigError(
            f"账号 {account.account_id} 的 expected_identity_fingerprint 必须是 16 位小写十六进制"
        )
    if account.profile_dir:
        profile = Path(account.profile_dir).expanduser().resolve()
        workspace = Path.cwd().resolve()
        if profile == workspace or workspace in profile.parents:
            raise DouyinConfigError(
                f"账号 {account.account_id} 的 profile_dir 不得位于项目目录内，以免提交登录态"
            )
    return account


def load_douyin_accounts(path: str | Path = "douyin_accounts.yaml") -> list[DouyinAccount]:
    target = Path(path)
    if not target.exists():
        return []
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise DouyinConfigError("配置文件顶层必须是对象")
    rows = raw.get("accounts", [])
    if not isinstance(rows, list):
        raise DouyinConfigError("accounts 必须是列表")
    if any(not isinstance(row, dict) for row in rows):
        raise DouyinConfigError("accounts 的每一项都必须是对象")
    accounts = [_build_account(row) for row in rows]
    ids = [account.account_id for account in accounts]
    if len(ids) != len(set(ids)):
        raise DouyinConfigError(f"account_id 重复：{ids}")
    return accounts
