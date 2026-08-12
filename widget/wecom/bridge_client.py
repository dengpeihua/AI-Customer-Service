from __future__ import annotations

import httpx


class BridgeError(RuntimeError):
    """与进程内 HTTP 桥通信失败。"""


class BridgeClient:
    """企微进程内 hook DLL 的本地 HTTP 桥客户端。

    骨架端点：GET /health、GET /ping、POST /echo、POST /shutdown。
    收发端点（契约已定，DLL 侧待 M4 偏移到位后实现）：
      POST /send        body {"contact_id","text"} -> {"ok","error","msg_id"}
      GET  /messages?since=<seq> -> {"ok","cursor","messages":[{seq,msg_id,contact_id,
                                     sender_id,text,is_group,at_me,timestamp}]}
    骨架 DLL 尚未实现这两个端点（404）；此时对应方法抛 BridgeError，由 adapter 降级处理。
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8752, token: str = "",
                 timeout: float = 3.0, client: httpx.Client | None = None):
        self.base_url = f"http://{host}:{port}"
        self._token = token
        self._client = client or httpx.Client(
            base_url=self.base_url, timeout=timeout, trust_env=False
        )

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    def health(self) -> dict:
        try:
            r = self._client.get("/health", headers=self._headers())
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            raise BridgeError(f"health failed: {e}") from e

    def ping(self) -> str:
        r = self._client.get("/ping", headers=self._headers())
        r.raise_for_status()
        return r.text.strip()

    def echo(self, data: str) -> str:
        """回显：验证双向数据通路（后续可作发送前的往返自检）。"""
        r = self._client.post("/echo", content=data.encode("utf-8"), headers=self._headers())
        r.raise_for_status()
        return r.text

    def send(self, contact_id: str, text: str) -> dict:
        """UI 路径发送（SetText+合成回车，需聚焦目标会话）。返回 {ok, error?, msg_id?}。
        json= 由 httpx 以 UTF-8 application/json 发出（中文无损）。骨架 DLL 未实现 /send（404）
        或通信失败 → 抛 BridgeError，交 adapter 转成诚实的 SendResult(ok=False)，绝不假装成功。
        注：自动回复应优先走 hsend（headless，可多客户并发、不抢前台）。"""
        try:
            r = self._client.post("/send", json={"contact_id": contact_id, "text": text},
                                   headers=self._headers())
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            raise BridgeError(f"send failed: {e}") from e
        except ValueError as e:                       # 非 JSON 响应
            raise BridgeError(f"send bad response: {e}") from e

    def hsend(self, contact_id: str, text: str, self_id: str = "") -> dict:
        """HEADLESS 数据层发送（不开窗/不聚焦，可多客户并发）——自动回复的生产通路。
        走合并 DLL 的 POST /hsend（headless_send → slot4 SendMessageToConversation）。
        返回 DLL 的 JSON 结果 {ok, ret0?, error?}。
        self_id 非空则一并下发；当前合并 DLL 用编译期 SELF_UID、会忽略该字段（前向兼容），
        native 侧把 SELF_UID 动态化后即自动生效，Python 无需再改。
        /hsend 未实现（404）或通信失败 → 抛 BridgeError，交 adapter 转成诚实 SendResult(ok=False)。"""
        payload = {"contact_id": contact_id, "text": text}
        if self_id:
            payload["self_id"] = self_id
        try:
            r = self._client.post("/hsend", json=payload, headers=self._headers())
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            raise BridgeError(f"hsend failed: {e}") from e
        except ValueError as e:                       # 非 JSON 响应
            raise BridgeError(f"hsend bad response: {e}") from e

    def fetch_messages(self, since: int) -> dict:
        """拉取 DLL recv-hook 捕获的入站消息（游标增量）。返回 {ok, cursor, messages:[...]}。
        骨架 DLL 未实现 /messages（404）→ 抛 BridgeError，由 poller 本轮当作无消息。"""
        try:
            r = self._client.get("/messages", params={"since": int(since)}, headers=self._headers())
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            raise BridgeError(f"fetch_messages failed: {e}") from e
        except ValueError as e:
            raise BridgeError(f"messages bad response: {e}") from e

    def fetch_collog(self, since: int) -> bytes:
        """拉 col_probe 被动收割的解密列值（企微本地库 message.db 明文）。返回**原始字节**——
        不 .json()：DLL 的 collog_json 把 >0x20 原始字节(含 UTF-8 尾字节)直塞进 JSON 串，
        非合法 UTF-8 JSON，须由 widget.wecom.msgdb 在字节层反转义。DLL 未实现 /collog(404)→抛。"""
        try:
            r = self._client.get("/collog", params={"since": int(since)}, headers=self._headers())
            r.raise_for_status()
            return r.content
        except httpx.HTTPError as e:
            raise BridgeError(f"fetch_collog failed: {e}") from e

    def list_dbs(self) -> list:
        """列出 col_hook 捕获到的活 sqlite3* 句柄（进程内自查用）。DLL 未实现→抛。"""
        try:
            r = self._client.get("/wecom_dbs", headers=self._headers())
            r.raise_for_status()
            return list(r.json().get("dbs", []))
        except httpx.HTTPError as e:
            raise BridgeError(f"list_dbs failed: {e}") from e

    def wecom_query(self, db: str, sql: str) -> dict:
        """进程内自查：在 db 上跑只读 SELECT 读企微本地库全量历史。返回 {ok,rows,nrows}。
        用 hex(content) 取二进制列时响应是合法 UTF-8；仍用字节层容错解析防偶发非法字节。"""
        try:
            r = self._client.post("/wecom_query", json={"db": db, "sql": sql}, headers=self._headers())
            r.raise_for_status()
            import json as _json
            return _json.loads(r.content.decode("utf-8", "replace"))
        except httpx.HTTPError as e:
            raise BridgeError(f"wecom_query failed: {e}") from e

    def is_ready(self) -> bool:
        try:
            return bool(self.health().get("ok"))
        except Exception:
            return False

    def shutdown(self) -> None:
        """请求 DLL 优雅卸载自身（best-effort，不抛）。"""
        try:
            self._client.post("/shutdown", headers=self._headers())
        except Exception:
            pass

    def close(self) -> None:
        self._client.close()
