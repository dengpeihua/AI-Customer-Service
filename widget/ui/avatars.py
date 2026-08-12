"""微信头像获取：本地缓存(head_image.db)优先 → contact.db 头像 URL 下载 → 首字母色块兜底。

resolve_bytes 是纯字节解析（可脱离 Qt 测）；get_pixmap 走 Qt，未命中先给占位、后台线程
异步解析，完成后 avatar_ready(wxid) 信号通知页面刷新该项。
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

import httpx
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPixmap

from widget.guarded_read import GuardRefused, adapter_fn_from, guarded_query, readable

_UNSET = object()
_REFUSE_BACKOFF_S = 2.0     # 闸拒绝后的短暂退避：别让每次重绘都为每个联系人再起一个线程
# 占位色块用的一组柔和颜色，按 wxid 哈希取一个，保证同一个人颜色稳定
_PALETTE = ["#12B3A6", "#2A78D6", "#E0A100", "#C0504D", "#8064A2", "#4BACC6", "#9BBB59"]


def _sql_str(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"     # 转义单引号，防注入/破坏 SQL


class AvatarProvider(QObject):
    avatar_ready = Signal(str)   # wxid：该头像已解析好，页面可重新 get_pixmap 拿真图

    def __init__(self, hook_base_url: str = "", client: httpx.Client | None = None,
                 size: int = 36, adapter=None,
                 adapter_fn: Optional[Callable[[], object]] = None):
        """★H3★ `adapter`/`adapter_fn` 给了就走带闸的 adapter 读库，本类不再持有任何 hook 地址。

        原来的写法把「主实例的端口」在启动时捕获成 `base_url` 字符串、之后永不改变：微信重启、
        端口易主之后，本店界面上摆的就是**抢到这个端口的陌生账号**的头像。现在读库经
        `adapter.query_db`（与收发同一道归属闸），闸不放行就退回占位色块。
        """
        super().__init__()
        self._adapter_fn = adapter_fn_from(adapter, adapter_fn)
        if self._adapter_fn is not None:
            # 这个 client 只用来下 contact.db 里那个**绝对** URL（腾讯 CDN）：
            # 读不到库就根本走不到这一步，所以它不绑任何 hook 地址。
            self._client = client or httpx.Client(timeout=8.0, trust_env=False)
        else:
            self._client = client or httpx.Client(
                base_url=hook_base_url, timeout=8.0, trust_env=False
            )
        self.size = size
        self._bytes: dict[str, bytes | None] = {}    # wxid -> 头像字节（None=确认无头像）
        self._pending: set[str] = set()
        self._refused_until = 0.0
        self._lock = threading.Lock()

    def set_adapter(self, adapter) -> None:
        """重连后重绑到新 adapter（见 `app.rebind_pages_after_reconnect`）。

        缓存不清：重连认领的是**同一个 channel_key = 同一个账号**，此前解析出来的头像仍然是他的。
        """
        if readable(adapter):      # 企微/降级壳子读不了个人微信库，不接（否则头像全变占位）
            self._adapter_fn = adapter_fn_from(adapter, None)
            with self._lock:
                self._refused_until = 0.0

    # ---- 纯字节解析（可测） ----
    def _query(self, db: str, sql: str) -> list[dict]:
        if self._adapter_fn is not None:
            return guarded_query(self._adapter_fn, db, sql)
        r = self._client.post("/QueryDB/execute", json={"optDbName": db, "SQL": sql})
        r.raise_for_status()
        return r.json().get("data", []) or []

    def resolve_bytes(self, wxid: str) -> bytes | None:
        with self._lock:
            if wxid in self._bytes:
                return self._bytes[wxid]
        try:
            data = self._do_resolve(wxid)
        except GuardRefused:
            # 端口易主/没接管：给占位色块，且**不写缓存** —— 否则这次拒绝会被当成
            # 「此人确认无头像」永久记住，重连之后真头像再也出不来。
            with self._lock:
                self._refused_until = time.monotonic() + _REFUSE_BACKOFF_S
            return None
        with self._lock:
            self._bytes[wxid] = data
        return data

    def _do_resolve(self, wxid: str) -> bytes | None:
        # 1) 本地缓存 head_image.db（瞬间、离线）
        try:
            rows = self._query("head_image.db",
                               f"SELECT HEX(image_buffer) AS hexbuf FROM head_image "
                               f"WHERE username={_sql_str(wxid)} LIMIT 1")
            hexbuf = rows[0].get("hexbuf") if rows else None
            if hexbuf:
                return bytes.fromhex(hexbuf)
        except GuardRefused:
            raise               # 「读到的是别人的」必须上浮成拒绝，不能被下面的兜底吞成 None
        except Exception:
            pass
        # 2) contact.db 头像 URL 下载
        try:
            rows = self._query("contact.db",
                               f"SELECT small_head_url FROM contact "
                               f"WHERE username={_sql_str(wxid)} LIMIT 1")
            url = rows[0].get("small_head_url") if rows else None
            if url:
                resp = self._client.get(url)
                if resp.status_code == 200 and resp.content:
                    return resp.content
        except GuardRefused:
            raise
        except Exception:
            pass
        return None

    # ---- Qt 头像（占位 + 异步） ----
    def get_pixmap(self, wxid: str, name: str = "") -> QPixmap:
        with self._lock:
            cached = self._bytes.get(wxid, _UNSET)
        if cached is _UNSET:
            self._request_async(wxid)
            return self._placeholder(name or wxid)
        if cached:
            pm = QPixmap()
            if pm.loadFromData(cached):
                return _circular(pm, self.size)
        return self._placeholder(name or wxid)

    def _request_async(self, wxid: str) -> None:
        with self._lock:
            if wxid in self._pending or time.monotonic() < self._refused_until:
                return          # 刚被闸拒过：短暂退避，别让一次列表重绘起一百个线程
            self._pending.add(wxid)

        def worker():
            try:
                self.resolve_bytes(wxid)
            finally:
                with self._lock:
                    self._pending.discard(wxid)
                self.avatar_ready.emit(wxid)

        threading.Thread(target=worker, daemon=True).start()

    def _placeholder(self, name: str) -> QPixmap:
        ch = (name or "?").strip()[:1] or "?"
        color = _PALETTE[hash(name) % len(_PALETTE)]
        pm = QPixmap(self.size, self.size); pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QBrush(QColor(color))); p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(0, 0, self.size, self.size)
        p.setPen(QColor("white")); f = QFont(); f.setPixelSize(int(self.size * 0.5)); f.setBold(True)
        p.setFont(f); p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, ch)
        p.end()
        return pm


def _circular(src: QPixmap, size: int) -> QPixmap:
    scaled = src.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                        Qt.TransformationMode.SmoothTransformation)
    out = QPixmap(size, size); out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out); p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QBrush(scaled)); p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(0, 0, size, size); p.end()
    return out
