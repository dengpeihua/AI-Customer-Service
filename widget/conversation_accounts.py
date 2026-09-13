"""工作台会话标识的轻量分类规则。"""
from __future__ import annotations


def _normalized(account_id: str) -> str:
    return str(account_id or "").strip().casefold()


def same_account(left: str, right: str) -> bool:
    a, b = _normalized(left), _normalized(right)
    return bool(a and b and a == b)


def is_group_account(account_id: str) -> bool:
    return _normalized(account_id).endswith("@chatroom")


def is_official_or_system_account(account_id: str) -> bool:
    value = _normalized(account_id)
    return value in {"system", "notification-system"}
