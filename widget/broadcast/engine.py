from __future__ import annotations

import random
import time
from typing import Callable

from widget.broadcast.contacts import ContactSource, Friend
from widget.broadcast.models import BroadcastTask
from widget.config import BroadcastConfig
from widget.sender import RateLimiter


def render_template(template: str, friend: Friend) -> str:
    name = friend.remark or friend.nick or "客户"
    return template.replace("{昵称}", name).replace("{备注}", friend.remark or "")


def remaining_recipients(task: BroadcastTask) -> list[str]:
    """还没发出去的收件人（已成功的排除），保持原名单顺序。再跑 run_broadcast 会发这些。"""
    sent = set(task.sent)
    return [w for w in task.recipients if w not in sent]


def is_resumable(task: BroadcastTask) -> bool:
    """任务是否可「继续发送」：已暂停且还有没发出去的收件人。"""
    return task.status == "paused" and bool(remaining_recipients(task))


def is_cancellable(task: BroadcastTask) -> bool:
    """任务是否可「取消」：仅未到点的定时任务（已排期）。"""
    return task.status == "scheduled"


def is_deletable(task: BroadcastTask) -> bool:
    """任务是否可「删除记录」：已结束的（完成或已取消），不删进行中/排期中的。"""
    return task.status in ("done", "cancelled")


def run_broadcast(
    task: BroadcastTask,
    source: ContactSource,
    adapter,
    bcfg: BroadcastConfig,
    *,
    limiter: RateLimiter,
    should_stop: Callable[[], bool],
    on_progress: Callable[[int, int, str], None] | None = None,
    save_fn: Callable[[BroadcastTask], None] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    rand_fn: Callable[[float, float], float] = random.uniform,
    now_fn: Callable[[], float] = time.time,
) -> BroadcastTask:
    friends = {f.wxid: f for f in source.list_friends()}
    blacklist = set(bcfg.blacklist)
    total = len(task.recipients)
    done = len(task.sent)
    task.status = "running"
    if save_fn:
        save_fn(task)

    sent_set = set(task.sent)
    batch_in_run = 0

    for wxid in task.recipients:
        if wxid in sent_set:
            continue                                   # 续跑：已成功的跳过（幂等）

        if should_stop():
            task.status = "paused"
            if save_fn:
                save_fn(task)
            return task

        task.failed = [f for f in task.failed if f[0] != wxid]  # 幂等：清掉该 wxid 的旧失败记录

        # 过滤：黑名单 / 非好友（含联系人库查不到）
        if wxid in blacklist:
            task.failed.append([wxid, "黑名单"])
            task.processed_at[wxid] = now_fn()
            done += 1
            if on_progress:
                on_progress(done, total, "failed:黑名单")
            if save_fn:
                save_fn(task)
            continue
        friend = friends.get(wxid)
        if friend is None or not friend.is_friend:
            task.failed.append([wxid, "非好友"])
            task.processed_at[wxid] = now_fn()
            done += 1
            if on_progress:
                on_progress(done, total, "failed:非好友")
            if save_fn:
                save_fn(task)
            continue

        # 日额度/分钟频控：触顶则暂停，剩余留待次日
        if not limiter.can_send():
            task.status = "paused"
            if save_fn:
                save_fn(task)
            return task

        if batch_in_run > 0 and batch_in_run % bcfg.batch_size == 0:
            sleep_fn(bcfg.batch_rest_s)                # 分批休息
        sleep_fn(rand_fn(bcfg.delay_min_s, bcfg.delay_max_s))  # 不同对象间隔

        res = adapter.send_message(wxid, render_template(task.template, friend),
                                   provenance="broadcast")   # 群发，溯源标 broadcast
        task.processed_at[wxid] = now_fn()
        done += 1
        batch_in_run += 1
        if res.ok:
            limiter.record()
            task.sent.append(wxid)
            sent_set.add(wxid)
            result = "sent"
        else:
            task.failed.append([wxid, "发送失败"])
            result = "failed:发送失败"
        if on_progress:
            on_progress(done, total, result)
        if save_fn:
            save_fn(task)

    task.status = "done"
    if save_fn:
        save_fn(task)
    return task
