from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Protocol

import httpx

from widget.config import WidgetConfig
from widget.guarded_read import GuardRefused, adapter_fn_from, guarded_query, readable


@dataclass
class Friend:
    wxid: str
    nick: str = ""
    remark: str = ""
    is_friend: bool = True


class ContactSource(Protocol):
    def list_friends(self) -> list[Friend]: ...


class FakeContactSource:
    def __init__(self, friends: list[Friend]):
        self._friends = list(friends)

    def list_friends(self) -> list[Friend]:
        return list(self._friends)


# 真实 contact.db 结构由 4.1.10.27 在线库坐实（2026-07-14）。QueryDB 返回值均为字符串。
_CONTACT_DB = "contact.db"
_CONTACT_SQL = "SELECT username, nick_name, remark, local_type, verify_flag FROM contact"

# 系统保留账号：local_type=1、verify_flag=0，但不是真人好友，须按 username 排除。
_SYSTEM_ACCOUNTS = frozenset({
    "filehelper", "fmessage", "medianote", "weixin", "newsapp", "notifymessage",
    "mphelper", "qqmail", "tmessage", "qmessage", "floatbottle",
    "notification_messages", "weixinreminder", "brandsessionholder",
})


class HookContactSource:
    """从个人微信 hook 的 contact.db 读好友名单。

    收件人名单和发送账号必须是**同一个号**：错位 = 拿甲号的好友表从乙号发出去
    （不是好友的发不出，两边都是好友的收到来自错号的群发）。

    ★H3★ 所以这里不再自己捧一个 hook 地址。原来的写法是启动时把「主实例的端口」捕获成
    `base_url` 字符串、之后永不改变；微信重启后端口易主（甲退出 → 乙抢到 30001，甲被重新
    认领到 30002），群发页就会列出**乙的私人好友**，再由甲的微信一条条发出去——发送端的
    归属闸全程为真，一次都不会响。改成：`adapter_fn()` 取此刻真正在收发的那个 adapter，
    查询走它带闸的 `query_db`；拿不到 / 闸不放行 → **空名单**
    （宁可群发名单是空的，也绝不列陌生人的联系人）。

    `base_url`/`client` 仅保留给直连口径（单元测试）；`adapter`/`adapter_fn` 一旦给了就优先。
    """

    def __init__(self, cfg: WidgetConfig, client: httpx.Client | None = None,
                 base_url: str | None = None, adapter=None,
                 adapter_fn: Optional[Callable[[], object]] = None):
        self.cfg = cfg
        self._adapter_fn = adapter_fn_from(adapter, adapter_fn)
        self.base_url = ""
        self._client = None
        if self._adapter_fn is None:
            self.base_url = base_url or cfg.hook_base_url
            self._client = client or httpx.Client(
                base_url=self.base_url, timeout=10.0, trust_env=False
            )

    def set_adapter(self, adapter) -> None:
        """重连后重绑到新 adapter（见 `app.rebind_pages_after_reconnect`）。
        读不了个人微信库的 adapter（企微 / 降级壳子）一律不接，免得把好好的名单换成空表。"""
        if readable(adapter):
            self._adapter_fn = adapter_fn_from(adapter, None)

    def _query(self, db_name: str, sql: str) -> list[dict]:
        if self._adapter_fn is not None:
            return guarded_query(self._adapter_fn, db_name, sql)
        r = self._client.post("/QueryDB/execute", json={"optDbName": db_name, "SQL": sql})
        r.raise_for_status()
        return r.json().get("data", []) or []

    def list_friends(self) -> list[Friend]:
        try:
            rows = self._query(_CONTACT_DB, _CONTACT_SQL)
        except GuardRefused:
            return []          # 端口易主 / 没接管：空名单，绝不端出陌生人的通讯录
        out: list[Friend] = []
        for row in rows:
            uname = str(row.get("username", "") or "")
            # 排除群 / 企业外部联系人 / 公众号 / 系统保留号
            if not uname or uname.endswith("@chatroom") or uname.endswith("@openim") \
                    or uname.startswith("gh_") or uname in _SYSTEM_ACCOUNTS:
                continue
            # 真好友：普通联系人(local_type==1) 且 未认证(verify_flag==0，认证/系统号为非 0)
            if str(row.get("local_type", "")) != "1":
                continue
            if str(row.get("verify_flag", "0") or "0") != "0":
                continue
            out.append(Friend(
                wxid=uname,
                nick=str(row.get("nick_name", "") or ""),
                remark=str(row.get("remark", "") or ""),
                is_friend=True,
            ))
        return out
