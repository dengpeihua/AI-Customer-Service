from __future__ import annotations

from typing import Mapping


def _compact_quote(value: object, limit: int = 120) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def quoted_reply_text(text: str, quoted_text: object) -> str:
    """Build a channel-independent visible quote for adapters without native quote APIs."""
    reply = str(text or "").strip()
    quote = _compact_quote(quoted_text)
    if not quote:
        return reply
    return f"「回复：{quote}」\n{reply}"


def send_reply(adapter, contact_id: str, text: str, reply_to: Mapping | None,
               provenance: str):
    """Send a reply bound to one inbound message, using native support when available."""
    if reply_to:
        native = getattr(adapter, "send_reply", None)
        if callable(native):
            return native(contact_id, text, dict(reply_to), provenance=provenance)
        text = quoted_reply_text(text, reply_to.get("text"))
    return adapter.send_message(contact_id, text, provenance=provenance)


def delivered_text(text: str, reply_to: Mapping | None) -> str:
    """Text shown in optimistic history; native adapters may later replace it from storage."""
    if not reply_to:
        return str(text or "").strip()
    return quoted_reply_text(text, reply_to.get("text"))
