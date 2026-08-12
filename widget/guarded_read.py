"""副表盘（群发名单 / 头像）的只读查询出口 —— 一律经「此刻活着的那个 adapter」的归属闸。

★H3★ 这两个读口原来各自捧着一个 `httpx.Client(base_url=...)`：启动时把端口字符串捕获一次、
之后永不改变。而端口是会易主的 —— 甲的微信退出，客户按文档 `pythonw start_wechat.py` 重开
并扫成乙，乙立刻占住腾出来的 30001，甲则被重新认领到 30002。于是：

  · 群发页列出的是**乙的私人好友**，再由甲的微信一条条把广告发过去
    （发送走的是甲自己的 adapter，归属闸全程为真、一次都不会响，用户看不到任何异常）；
  · 头像页把陌生人的头像摆进本店的界面。

所以它们不许自己持有 URL：查询必须走 `adapter.query_db`，和收发共用同一道闸。
拿不到可信 adapter（没接管 / 是降级壳子 / 端口已易主）就**空手而归** ——
「群发名单是空的」用户一眼看得出不对，「列的是陌生人的通讯录」看不出来。
"""
from __future__ import annotations

from typing import Callable, Optional

from widget.adapters.wechat_hook import PortOwnershipLost


class GuardRefused(RuntimeError):
    """此刻没有可信的读来源（没接管 / 降级壳子 / 端口易主）：调用方必须返回空。"""


def readable(adapter) -> bool:
    """这个 adapter 能不能当带闸的只读来源。

    降级壳子（UnavailableWeChatAdapter）和企微 adapter 都没有 `query_db` —— 它们读不了
    个人微信那几张库，也就不该被当成名单/头像的来源（重绑时同样按这条挡住）。
    """
    return callable(getattr(adapter, "query_db", None))


def live_adapter(adapter_fn: Optional[Callable[[], object]]):
    """取此刻真正在用的那个 adapter；取不到、或它没有带闸的只读出口，返回 None。"""
    if adapter_fn is None:
        return None
    try:
        adapter = adapter_fn()
    except Exception:                       # noqa: BLE001 —— 取不到 = 没有，绝不猜一个端口
        return None
    return adapter if readable(adapter) else None


def guarded_query(adapter_fn: Optional[Callable[[], object]],
                  db_name: str, sql: str) -> list[dict]:
    """经当前 adapter 的归属闸做一次只读查询。闸不放行/没有 adapter → GuardRefused。

    真正的查询失败（HTTP 错误、QueryDB 报错）照旧向上抛：那是「读不到」，
    与「读到的是别人的」是两回事，不该被同一个兜底吞掉。
    """
    adapter = live_adapter(adapter_fn)
    if adapter is None:
        raise GuardRefused("当前没有已验证归属的微信 adapter：拒绝读取（绝不读别人的微信）")
    try:
        return adapter.query_db(db_name, sql)
    except PortOwnershipLost as e:
        raise GuardRefused(str(e)) from e


def adapter_fn_from(adapter=None, adapter_fn=None) -> Optional[Callable[[], object]]:
    """构造参数归一：给了 `adapter_fn` 用它（能跟着重连走），只给 `adapter` 就包成常量。"""
    if adapter_fn is not None:
        return adapter_fn
    if adapter is not None:
        return lambda: adapter
    return None
