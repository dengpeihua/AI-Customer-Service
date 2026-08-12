"""个人微信账号/会话 ID 的统一分类规则。

微信的 SessionTable、消息库和 hook 事件会用不同 ID 表示同一类对象。这里集中维护
群聊、公众号和系统占位账号的边界，避免会话列表与入站过滤各写一套后逐渐漂移。
"""
from __future__ import annotations


_SYSTEM_ACCOUNT_IDS = frozenset({
    "filehelper", "filehelper_system", "fmessage", "medianote", "weixin",
    "newsapp", "notifymessage", "mphelper", "qqmail", "tmessage",
    "qmessage", "floatbottle", "notification_messages", "weixinreminder",
    # 微信把订阅号/服务号聚合成占位会话；真正的公众号发送者通常另有 gh_ ID。
    "brandsessionholder", "brandservicesessionholder",
})


def _normalized(account_id: str) -> str:
    return str(account_id or "").strip().casefold()


def same_account(left: str, right: str) -> bool:
    """账号 ID 比较统一忽略大小写；空 ID 永远不能证明是同一账号。"""
    a, b = _normalized(left), _normalized(right)
    return bool(a and b and a == b)


def is_group_account(account_id: str) -> bool:
    return _normalized(account_id).endswith("@chatroom")


def is_official_or_system_account(account_id: str) -> bool:
    """公众号（gh_）及微信系统/聚合占位账号。"""
    value = _normalized(account_id)
    return bool(value) and (value.startswith("gh_") or value in _SYSTEM_ACCOUNT_IDS)


def is_supported_conversation(account_id: str) -> bool:
    """工作台可展示/处理的个人微信会话：普通好友或微信群。"""
    value = _normalized(account_id)
    return bool(value) and not value.endswith("@openim") \
        and not is_official_or_system_account(value)


def is_private_conversation(account_id: str) -> bool:
    return is_supported_conversation(account_id) and not is_group_account(account_id)
