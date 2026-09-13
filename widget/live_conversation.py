from __future__ import annotations

from typing import Iterable


def event_to_history_message(event: dict) -> dict:
    outbound = event.get("direction") == "outbound"
    kind = str(event.get("kind") or "text")
    if event.get("quote_text"):
        kind = "quote"
    return {
        "local_id": str(event.get("event_id") or ""),
        "kind": kind,
        "text": str(event.get("text") or ""),
        "sender_id": str(event.get("sender_id") or ("self" if outbound else event.get("contact_id") or "")),
        "sender_name": "我" if outbound else str(event.get("sender_name") or "客户"),
        "sender_avatar": "" if outbound else str(event.get("sender_avatar") or ""),
        "media_url": str(event.get("media_url") or ""),
        "title": str(event.get("title") or ""),
        "description": str(event.get("description") or ""),
        "url": str(event.get("url") or ""),
        "display_time": str(event.get("display_time") or ""),
        "video_url": str(event.get("video_url") or ""),
        "quote_sender": str(event.get("quote_sender") or ""),
        "quote_text": str(event.get("quote_text") or ""),
        "is_self": outbound,
        "is_group": bool(event.get("is_group", False)),
        "ts": int(event.get("timestamp") or 0),
        "provenance": str(event.get("provenance") or ("agent" if outbound else "customer")),
        "live_event_id": str(event.get("event_id") or ""),
    }


def _same_message(left: dict, right: dict) -> bool:
    if bool(left.get("is_self")) != bool(right.get("is_self")):
        return False
    if str(left.get("text") or "") != str(right.get("text") or ""):
        return False
    if str(left.get("kind") or "text") != str(right.get("kind") or "text"):
        return False
    left_media = str(left.get("media_url") or "")
    right_media = str(right.get("media_url") or "")
    if left_media and right_media and left_media != right_media:
        return False
    left_ts = int(left.get("ts") or 0)
    right_ts = int(right.get("ts") or 0)
    return not left_ts or not right_ts or abs(left_ts - right_ts) <= 5


def merge_live_messages(stored: Iterable[dict], live: Iterable[dict], limit: int = 200) -> list[dict]:
    """Overlay not-yet-persisted events without duplicating their later database copies."""
    merged = [dict(message) for message in stored]
    stored_count = len(merged)
    known_live_ids = {str(message.get("live_event_id") or "") for message in merged}
    matched_stored: set[int] = set()
    for optimistic in live:
        event_id = str(optimistic.get("live_event_id") or "")
        if event_id and event_id in known_live_ids:
            continue
        match = next((index for index, saved in enumerate(merged[:stored_count])
                      if index not in matched_stored and _same_message(saved, optimistic)), None)
        if match is not None:
            matched_stored.add(match)
            continue
        merged.append(dict(optimistic))
        if event_id:
            known_live_ids.add(event_id)
    # Stored channel history is already in authoritative visual order. DOM snapshots often
    # have no machine timestamp, so sorting by local UUID would randomly reorder the chat.
    return merged[-limit:] if limit and len(merged) > limit else merged


def merge_history_snapshots(
    primary: Iterable[dict], secondary: Iterable[dict], limit: int = 200,
) -> list[dict]:
    """Merge the enterprise-channel snapshot with the backend fallback snapshot.

    Exact/near-time copies are collapsed one-to-one. When both sources contain the same
    message, the primary channel history keeps its richer media/local id.
    """
    left = [dict(message) for message in primary]
    right = [dict(message) for message in secondary]
    # Prefer the snapshot with channel-local ids/richer message fields. If neither side is
    # distinguishable, use a deterministic fingerprint so network completion order is irrelevant.
    def authority(messages: list[dict]) -> tuple[int, int, str]:
        local_rows = sum(
            1 for message in messages
            if str(message.get("local_id") or "").startswith(("douyin:", "legacy:"))
        )
        rich_fields = sum(
            1 for message in messages
            for key in ("media_path", "media_url", "sender_avatar", "sender_name", "is_group")
            if message.get(key)
        )
        fingerprint = "\x1f".join(
            f"{int(message.get('ts') or 0)}:{int(bool(message.get('is_self')))}:"
            f"{str(message.get('text') or '')}:{str(message.get('local_id') or '')}"
            for message in messages
        )
        return local_rows, rich_fields, fingerprint

    if authority(right) > authority(left):
        left, right = right, left
    merged = left
    primary_count = len(merged)
    matched_primary: set[int] = set()
    for fallback in right:
        match = next((
            index for index, saved in enumerate(merged[:primary_count])
            if index not in matched_primary and _same_message(saved, fallback)
        ), None)
        if match is not None:
            matched_primary.add(match)
            # Backend carries durable provenance/delivery fields that the local DB lacks.
            for key in ("provenance", "delivery_status"):
                if fallback.get(key) and not merged[match].get(key):
                    merged[match][key] = fallback[key]
            continue
        merged.append(dict(fallback))
    merged.sort(key=lambda message: (int(message.get("ts") or 0), str(message.get("local_id") or "")))
    return merged[-limit:] if limit and len(merged) > limit else merged
