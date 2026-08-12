from __future__ import annotations
import time
import httpx
from app.config import settings


class WeComApiError(Exception):
    pass


class WeComClient:
    def __init__(self, client: httpx.Client | None = None):
        self._client = client or httpx.Client(base_url=settings.wecom_api_base, timeout=10.0)
        self._tokens: dict[str, tuple[str, float]] = {}   # corp_id -> (token, expire_at)

    def get_access_token(self, corp_id: str, secret: str) -> str:
        cached = self._tokens.get(corp_id)
        if cached and cached[1] > time.time() + 60:
            return cached[0]
        try:
            r = self._client.get("/cgi-bin/gettoken", params={"corpid": corp_id, "corpsecret": secret})
            r.raise_for_status(); d = r.json()
        except httpx.HTTPError as e:
            raise WeComApiError(f"wecom api failed: {type(e).__name__}") from None
        if "access_token" not in d:
            raise WeComApiError(f"gettoken failed errcode={d.get('errcode')}")
        tok = d["access_token"]
        self._tokens[corp_id] = (tok, time.time() + d.get("expires_in", 7200))
        return tok

    def sync_msg(self, access_token, token, open_kfid, cursor) -> dict:
        try:
            r = self._client.post("/cgi-bin/kf/sync_msg", params={"access_token": access_token},
                json={"cursor": cursor, "token": token, "open_kfid": open_kfid, "limit": 1000})
            r.raise_for_status(); return r.json()
        except httpx.HTTPError as e:
            raise WeComApiError(f"wecom api failed: {type(e).__name__}") from None

    def send_text(self, access_token, open_kfid, touser, content) -> bool:
        try:
            r = self._client.post("/cgi-bin/kf/send_msg", params={"access_token": access_token},
                json={"touser": touser, "open_kfid": open_kfid, "msgtype": "text",
                      "text": {"content": content}})
            r.raise_for_status(); return r.json().get("errcode", 0) == 0
        except httpx.HTTPError as e:
            raise WeComApiError(f"wecom api failed: {type(e).__name__}") from None


class FakeWeComClient:
    def __init__(self, msg_list=None, next_cursor="CURSOR2"):
        self.sent: list[tuple] = []
        self._msg_list = msg_list or []
        self._next = next_cursor

    def get_access_token(self, corp_id, secret) -> str:
        return "fake-token"

    def sync_msg(self, access_token, token, open_kfid, cursor) -> dict:
        return {"errcode": 0, "msg_list": self._msg_list, "next_cursor": self._next, "has_more": 0}

    def send_text(self, access_token, open_kfid, touser, content) -> bool:
        self.sent.append((open_kfid, touser, content)); return True
