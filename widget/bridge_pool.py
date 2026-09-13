# widget/bridge_pool.py
"""多租户 N-Bridge 池：按 (backend, tenant_id, login) 去重缓存 Bridge。

隔离 = token，token = Bridge 登录用的 cfg（见 spec §2）。给每个账号一个用它自己
tenant/login 登录的 Bridge，后端既有 token 作用域即隔离数据（后端零改）。

去重键 (backend, tenant_id, login)：同租户账号共用一个 Bridge/token
⇒ legacy/单租户所有实例共用一个 Bridge，与旧版 `Bridge(cfg)` 逐字节等价（零回归）；
不同租户各自独立 Bridge ⇒ 隔离。
"""
from __future__ import annotations

import dataclasses
from typing import Callable, Optional


def _tenant_from_jwt(token: str) -> Optional[int]:
    """只读 JWT payload 的 tenant_id claim（不验签，仅取值告警用）。取不到返回 None。"""
    import base64
    import json
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        d = json.loads(base64.urlsafe_b64decode(payload))
        t = d.get("tenant_id")
        return int(t) if t is not None else None
    except Exception:                                # noqa: BLE001
        return None


class BridgePool:
    def __init__(self, base_cfg, *,
                 bridge_factory: Optional[Callable[[object], object]] = None,
                 log: Optional[Callable[[str], None]] = None):
        self._base = base_cfg
        self._factory = bridge_factory or self._default_factory
        self._log = log or (lambda m: None)
        self._cache: dict[tuple, object] = {}

    @staticmethod
    def _default_factory(cfg):
        from widget.bridge import Bridge
        return Bridge(cfg)

    def _key(self, cfg) -> tuple:
        return (getattr(cfg, "backend_base_url", ""), int(getattr(cfg, "tenant_id", 0) or 0),
                getattr(cfg, "login", ""))

    def _get(self, cfg):
        key = self._key(cfg)
        b = self._cache.get(key)
        if b is None:
            b = self._factory(cfg)
            self._cache[key] = b
        return b

    def for_config(self, cfg):
        return self._get(cfg)

    def for_instance(self, inst):
        # per-instance cfg 视图：共享 base 的 backend/其它设置，覆写这个账号的租户凭据。
        tenant_id = int(getattr(inst, "tenant_id", 0) or 0)
        login = getattr(inst, "login", "") or ""
        password = getattr(inst, "password", "") or ""
        password_enc = getattr(inst, "password_enc", "") or ""
        same_identity = (
            tenant_id == int(getattr(self._base, "tenant_id", 0) or 0)
            and login == (getattr(self._base, "login", "") or "")
        )
        if not password and not password_enc and same_identity:
            password = getattr(self._base, "password", "") or ""
            password_enc = getattr(self._base, "password_enc", "") or ""
        if not password and not password_enc:
            raise ValueError(
                f"抖音后端账号 tenant_id={tenant_id} login={login!r} 没有独立凭据，"
                "且不能继承全局账号凭据"
            )
        cfg = dataclasses.replace(
            self._base,
            tenant_id=tenant_id,
            login=login,
            password=password,
            password_enc=password_enc,
        )
        return self._get(cfg)

    def all(self) -> list:
        return list(self._cache.values())

    def warn_if_tenant_mismatch(self, cfg, token: str) -> None:
        """登录后校验：JWT 的 tenant 与 config 声明不一致 → 告警（配置写错/login 属别的租户）。"""
        actual = _tenant_from_jwt(token)
        want = int(getattr(cfg, "tenant_id", 0) or 0)
        if actual is not None and want and actual != want:
            self._log(f"[bridge-pool] ⚠️ 租户不一致：配置 tenant_id={want}，但登录 token 的租户是 "
                      f"{actual}（login={getattr(cfg, 'login', '')} 可能属于别的租户）")
