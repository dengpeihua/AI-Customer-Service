"""后台登录失败限流（滑动窗口，进程内）。

两道闸：按账号（挡单账号爆破）+ 按来源 IP（挡同一台机器扫多个账号的撞库）。
锁定期内即使密码正确也不放行，否则爆破者只要最后一次猜对就绕过了限流。
"""
import time
from collections import OrderedDict, deque

from app.config import settings


class LoginLimiter:
    def __init__(self, max_attempts: int, window_s: int, max_keys: int = 1000):
        self.max_attempts = max_attempts
        self.window_s = window_s
        self.max_keys = max_keys
        self.now_fn = time.time
        self._fails: OrderedDict[str, deque[float]] = OrderedDict()

    def _prune(self, key: str) -> deque[float]:
        cutoff = self.now_fn() - self.window_s
        hits = self._fails.get(key) or deque()
        while hits and hits[0] <= cutoff:
            hits.popleft()
        return hits

    def is_locked(self, key: str) -> bool:
        return len(self._prune(key)) >= self.max_attempts

    def record_failure(self, key: str) -> None:
        hits = self._prune(key)
        hits.append(self.now_fn())
        self._fails[key] = hits
        self._fails.move_to_end(key)
        while len(self._fails) > self.max_keys:  # 有界，防止被随机 key 撑爆内存
            self._fails.popitem(last=False)

    def reset(self, key: str) -> None:
        self._fails.pop(key, None)

    def clear(self) -> None:
        self._fails.clear()
        self.now_fn = time.time


account_limiter = LoginLimiter(settings.admin_login_max_attempts, settings.admin_login_window_s)
ip_limiter = LoginLimiter(settings.admin_login_ip_max_attempts, settings.admin_login_window_s)


def reset_login_limiters() -> None:
    account_limiter.clear()
    ip_limiter.clear()
