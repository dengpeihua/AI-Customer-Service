from __future__ import annotations
from typing import Callable
from widget.models import InboundMsg, SendResult
from widget.reply_quote import quoted_reply_text

class FakeWeChatAdapter:
    channel = "wechat_personal"
    def __init__(self, self_id: str = "wxid_me", channel_key: str = "wechat_personal"):
        self.channel = channel_key
        self._self_id = self_id
        self._on_message: Callable[[InboundMsg], None] | None = None
        self.sent: list[tuple[str, str]] = []
        self.sent_provenance: list[str] = []      # 与 sent 同序，记录每条的溯源来源
    def start(self, on_message: Callable[[InboundMsg], None]) -> None:
        self._on_message = on_message
    def inject(self, msg: InboundMsg) -> None:
        if self._on_message:
            self._on_message(msg)
    def send_message(self, contact_id: str, text: str, provenance: str = "human") -> SendResult:
        self.sent.append((contact_id, text))
        self.sent_provenance.append(provenance)
        return SendResult(ok=True)
    def send_reply(self, contact_id: str, text: str, reply_to: InboundMsg,
                   provenance: str = "human") -> SendResult:
        return self.send_message(
            contact_id, quoted_reply_text(text, reply_to.get("text")), provenance=provenance
        )
    def provenance_for(self, text: str) -> str | None:
        return None
    def self_wxid(self) -> str:
        return self._self_id
    def stop(self) -> None:
        self._on_message = None
