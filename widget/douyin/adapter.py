from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import urlsplit

from app.channels.douyin.browser_client import (
    DouyinBrowserImClient,
    DouyinConversation,
    DouyinConversationHistory,
    DouyinDeliveryReceipt,
    DouyinInboundEvent,
)
from app.channels.douyin.config import DouyinAccount
from widget.models import InboundMsg, SendResult


class DouyinAdapter:
    """Bridge one personal Douyin inbox into the existing customer-service pipeline."""

    _HISTORY_LIMIT = 500
    _DELIVERY_LEASE_SECONDS = 300

    @staticmethod
    def _safe_content_url(value: object) -> str:
        url = str(value or "").strip()
        try:
            parsed = urlsplit(url)
        except ValueError:
            return ""
        host = (parsed.hostname or "").lower()
        allowed_host = host == "douyin.com" or host.endswith(".douyin.com")
        return url if (
            parsed.scheme == "https" and allowed_host and parsed.path not in {"", "/"}
        ) else ""

    def __init__(
        self,
        account: DouyinAccount,
        client: DouyinBrowserImClient | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.account = account
        self.channel = account.channel_key
        self._client = client or DouyinBrowserImClient(account, clock=clock)
        self._clock = clock
        self._on_message: Callable[[InboundMsg], str | None] | None = None
        self._stop = threading.Event()
        self._events: queue.Queue[DouyinInboundEvent | None] = queue.Queue()
        self._dispatch_thread: threading.Thread | None = None
        self._state_lock = threading.RLock()
        self._seen_by_conversation: dict[str, set[str]] = {}
        self._legacy_processed: set[str] = set()
        self._targets: dict[str, dict] = {}
        self._contacts: dict[str, str] = {}
        self._previews: dict[str, str] = {}
        self._avatars: dict[str, str] = {}
        self._history: dict[str, list[dict]] = {}
        self._pending_events: dict[str, dict] = {}
        self._queued_pending: set[str] = set()
        self._had_persisted_baseline = False
        self._state_path = account.resolved_data_dir / "private_message_state.json"
        self._local_error = ""
        self.last_error = ""
        self.healthy = False
        self._load_state()

    def self_wxid(self) -> str:
        return f"douyin-account:{self.account.account_id}"

    def start(self, on_message: Callable[[InboundMsg], str | None]) -> None:
        if self._dispatch_thread is not None:
            return
        self._on_message = on_message
        self._stop.clear()
        self._dispatch_thread = threading.Thread(
            target=self._dispatch_loop,
            name=f"douyin-dispatch-{self.account.account_id}",
            daemon=True,
        )
        self._dispatch_thread.start()
        try:
            self._client.start(
                self._enqueue_event,
                self._upsert_conversation,
                self._upsert_history_snapshot,
            )
            self._queue_due_pending(force=True)
        except Exception:
            self._stop.set()
            self._events.put(None)
            self._dispatch_thread.join(timeout=2.0)
            self._dispatch_thread = None
            self._on_message = None
            raise

    def stop(self) -> None:
        self._stop.set()
        try:
            self._client.stop()
        finally:
            self._events.put(None)
            if self._dispatch_thread is not None:
                self._dispatch_thread.join(timeout=3.0)
                self._dispatch_thread = None
            self._save_state()
            self._on_message = None
            self._refresh_health()

    def _enqueue_event(self, event: DouyinInboundEvent) -> None:
        if not self._stop.is_set():
            self._events.put(event)

    def _upsert_conversation(self, conversation: DouyinConversation) -> None:
        contact_id = f"im:{conversation.conversation_id}"
        with self._state_lock:
            target = self._safe_target(conversation.target)
            name = conversation.name or conversation.conversation_id
            preview = str(conversation.preview or "")[:500]
            avatar_url = str(conversation.avatar_url or "")[:2000]
            legacy_contacts = [
                existing_contact
                for existing_contact, existing_target in self._targets.items()
                if existing_contact != contact_id
                and str(existing_target.get("locator_kind") or "") == "conversation_item"
                and int(existing_target.get("index") or 0) == int(target.get("index") or 0)
                and next(
                    (
                        line.strip()
                        for line in str(self._contacts.get(existing_contact) or "").splitlines()
                        if line.strip()
                    ),
                    "",
                ) == name
            ]
            if contact_id not in self._contacts and len(legacy_contacts) == 1:
                self._migrate_contact_locked(legacy_contacts[0], contact_id)
            changed = (
                self._targets.get(contact_id) != target
                or self._contacts.get(contact_id) != name
                or self._previews.get(contact_id) != preview
                or self._avatars.get(contact_id) != avatar_url
            )
            self._targets[contact_id] = target
            self._contacts[contact_id] = name
            self._previews[contact_id] = preview
            self._avatars[contact_id] = avatar_url
            if changed:
                self._save_state_locked()

    def _upsert_history_snapshot(self, snapshot: DouyinConversationHistory) -> None:
        contact_id = f"im:{snapshot.conversation_id}"
        self._replace_remote_history(contact_id, snapshot.messages)

    def _migrate_contact_locked(self, old_contact: str, new_contact: str) -> None:
        old_conversation = old_contact.removeprefix("im:")
        new_conversation = new_contact.removeprefix("im:")
        for mapping in (self._targets, self._contacts, self._previews, self._avatars):
            value = mapping.pop(old_contact, None)
            if value is not None and new_contact not in mapping:
                mapping[new_contact] = value
        old_rows = self._history.pop(old_contact, [])
        if old_rows:
            rows = self._history.setdefault(new_contact, [])
            existing = {str(row.get("local_id") or "") for row in rows}
            rows[:0] = [
                row for row in old_rows
                if str(row.get("local_id") or "") not in existing
            ]
            del rows[:-self._HISTORY_LIMIT]
        old_seen = self._seen_by_conversation.pop(old_conversation, set())
        if old_seen:
            self._seen_by_conversation.setdefault(new_conversation, set()).update(old_seen)
        for pending in self._pending_events.values():
            if not isinstance(pending, dict):
                continue
            if str(pending.get("conversation_id") or "") == old_conversation:
                pending["conversation_id"] = new_conversation
                pending["target"] = dict(self._targets.get(new_contact) or {})

    def _dispatch_loop(self) -> None:
        while True:
            try:
                event = self._events.get(timeout=1.0)
            except queue.Empty:
                self._queue_due_pending(force=False)
                continue
            if event is None:
                break
            try:
                self._accept_event(event)
                self._local_error = ""
            except Exception as exc:  # noqa: BLE001 - keep later messages available
                self._local_error = str(exc)[:500]
                self._postpone_event(event, delay_s=5.0)
            self._refresh_health()

    def _accept_event(self, event: DouyinInboundEvent) -> None:
        msg_id = f"douyin:{event.message_key}"
        contact_id = f"im:{event.conversation_id}"
        with self._state_lock:
            was_queued = msg_id in self._queued_pending
            self._queued_pending.discard(msg_id)
            seen = self._seen_by_conversation.setdefault(event.conversation_id, set())
            if event.message_key in seen:
                return
            if msg_id in self._legacy_processed:
                self._legacy_processed.discard(msg_id)
                self._mark_processed_locked(event.conversation_id, event.message_key)
                self._save_state_locked()
                return
            prior_pending = self._pending_events.get(msg_id)
            if isinstance(prior_pending, dict):
                retry_at = float(prior_pending.get("retry_at") or 0)
                if retry_at > self._clock() and not was_queued:
                    return
            self._targets[contact_id] = self._safe_target(event.target)
            self._contacts[contact_id] = event.sender_name or event.sender_id or contact_id
            message = InboundMsg(
                channel=self.channel,
                msg_id=msg_id,
                contact_id=contact_id,
                sender_id=event.sender_id or event.conversation_id,
                sender_name=event.sender_name or event.sender_id,
                sender_avatar=event.sender_avatar,
                text=event.text,
                kind=event.kind,
                media_url=event.media_url,
                title=event.title,
                description=event.description,
                url=event.url,
                display_time=event.display_time,
                is_group=False,
                at_me=False,
                timestamp=int(event.timestamp or self._clock()),
                source_type="douyin_private_message",
                delivery_replay=isinstance(prior_pending, dict),
            )
            self._record_message_locked(message, is_self=False, provenance="customer")
            if (
                event.initial_scan
                and not self.account.process_existing_messages
                and not self._had_persisted_baseline
            ):
                self._mark_processed_locked(event.conversation_id, event.message_key)
                self._save_state_locked()
                return
            pending = self._event_to_dict(event)
            pending["retry_at"] = 0
            self._pending_events[msg_id] = pending
            self._save_state_locked()
        callback = self._on_message
        outcome = None
        if callback is not None:
            outcome = callback(message)
        if outcome in {"error", "delivery_uncertain", "delivery_waiting"}:
            if outcome == "delivery_uncertain":
                delay = self._DELIVERY_LEASE_SECONDS + 10
            elif outcome == "delivery_waiting":
                delay = 30
            else:
                delay = 5
            self._postpone_event(event, delay_s=delay)
            return
        with self._state_lock:
            self._mark_processed_locked(event.conversation_id, event.message_key)
            self._pending_events.pop(msg_id, None)
            self._save_state_locked()

    def _postpone_event(self, event: DouyinInboundEvent, *, delay_s: float) -> None:
        msg_id = f"douyin:{event.message_key}"
        with self._state_lock:
            pending = dict(self._pending_events.get(msg_id) or self._event_to_dict(event))
            pending["retry_at"] = self._clock() + max(1.0, float(delay_s))
            self._pending_events[msg_id] = pending
            self._save_state_locked()

    def _queue_due_pending(self, *, force: bool) -> None:
        now = self._clock()
        with self._state_lock:
            pending = []
            # Move the deadline forward before queueing so the one-second dispatcher
            # tick cannot enqueue the same persistent event repeatedly.
            for msg_id, value in self._pending_events.items():
                if not isinstance(value, dict):
                    continue
                if msg_id in self._queued_pending:
                    continue
                if force or float(value.get("retry_at") or 0) <= now:
                    event = self._event_from_dict(value)
                    if event is not None:
                        pending.append(event)
                        self._queued_pending.add(msg_id)
            if pending:
                self._save_state_locked()
        for event in pending:
            if event is not None and not self._stop.is_set():
                self._events.put(event)

    @staticmethod
    def _safe_target(target: dict) -> dict:
        return {
            "uid": str(target.get("uid") or "")[:256],
            "name": str(target.get("name") or "")[:256],
            "index": max(0, int(target.get("index") or 0)),
            "locator_kind": str(target.get("locator_kind") or "stable_uid")[:64],
            "list_scope": str(target.get("list_scope") or "inbox")[:32],
        }

    @classmethod
    def _event_to_dict(cls, event: DouyinInboundEvent) -> dict:
        return {
            "message_key": event.message_key,
            "conversation_id": event.conversation_id,
            "sender_id": event.sender_id,
            "sender_name": event.sender_name,
            "text": event.text,
            "kind": event.kind,
            "media_url": event.media_url,
            "title": event.title,
            "description": event.description,
            "url": event.url,
            "display_time": event.display_time,
            "sender_avatar": event.sender_avatar,
            "timestamp": int(event.timestamp),
            "target": cls._safe_target(event.target),
            "initial_scan": bool(event.initial_scan),
        }

    @classmethod
    def _event_from_dict(cls, value: dict) -> DouyinInboundEvent | None:
        try:
            return DouyinInboundEvent(
                message_key=str(value["message_key"]),
                conversation_id=str(value["conversation_id"]),
                sender_id=str(value.get("sender_id") or ""),
                sender_name=str(value.get("sender_name") or ""),
                text=str(value["text"]),
                kind=str(value.get("kind") or "text"),
                media_url=str(value.get("media_url") or ""),
                title=str(value.get("title") or ""),
                description=str(value.get("description") or ""),
                url=str(value.get("url") or ""),
                display_time=str(value.get("display_time") or ""),
                sender_avatar=str(value.get("sender_avatar") or ""),
                timestamp=int(value["timestamp"]),
                target=cls._safe_target(dict(value.get("target") or {})),
                initial_scan=bool(value.get("initial_scan")),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def send_message(self, contact_id: str, text: str, provenance: str = "human") -> SendResult:
        if not self.account.send_enabled:
            return SendResult(ok=False, error="该抖音账号未启用真实发送（send_enabled=false）")
        with self._state_lock:
            target = dict(self._targets.get(contact_id) or {})
        if not target:
            return SendResult(ok=False, error="找不到该抖音会话的页面定位信息，请先收到或扫描一条私信")
        is_dom_target = str(target.get("locator_kind") or "stable_uid") == "conversation_item"
        try:
            receipt = self._client.send_text(
                target,
                text,
                # Sender only reaches the AI path after the global auto_send gate;
                # this adapter separately enforces the account send_enabled gate.
                # AI must match the saved DOM fingerprint exactly. Only an explicit
                # human reply may recover from a rotated fingerprint by traversing
                # the full list and proving the display name globally unique.
                allow_dom_target=is_dom_target and provenance in {"human", "ai"},
                allow_dom_name_fallback=is_dom_target and provenance == "human",
            )
        except Exception as exc:  # noqa: BLE001 - surface browser/protocol failure
            self._local_error = str(exc)[:500]
            self._refresh_health()
            return SendResult(ok=False, error=str(exc))
        result = self._receipt_result(receipt)
        if not result.ok:
            self._local_error = result.error[:500]
            self._refresh_health()
            return result
        message = InboundMsg(
            channel=self.channel,
            msg_id=f"local:{receipt.client_message_id}",
            contact_id=contact_id,
            sender_id=self.self_wxid(),
            text=text,
            is_group=False,
            at_me=False,
            timestamp=int(self._clock()),
            source_type="douyin_private_message",
        )
        with self._state_lock:
            self._record_message_locked(message, is_self=True, provenance=provenance)
            self._save_state_locked()
        self._local_error = ""
        self._refresh_health()
        return result

    @staticmethod
    def _receipt_result(receipt: DouyinDeliveryReceipt) -> SendResult:
        if receipt.status == "confirmed":
            return SendResult(ok=True)
        return SendResult(
            ok=False,
            error=receipt.error or f"抖音发送回执状态不可确认：{receipt.status}",
            uncertain=receipt.status == "unknown",
        )

    def send_reply(
        self,
        contact_id: str,
        text: str,
        reply_to: InboundMsg,
        provenance: str = "human",
    ) -> SendResult:
        del reply_to
        return self.send_message(contact_id, text, provenance)

    def scan_now(self) -> int:
        try:
            count = self._client.scan_now()
            self._local_error = ""
            return count
        except Exception as exc:  # noqa: BLE001
            self._local_error = str(exc)[:500]
            raise
        finally:
            self._refresh_health()

    def status_snapshot(self) -> dict:
        self._refresh_health()
        status_fn = getattr(self._client, "status", None)
        browser = status_fn() if callable(status_fn) else {}
        return {
            **browser,
            "account_id": self.account.account_id,
            "display_name": self.account.display_name,
            "channel": self.channel,
            "healthy": self.healthy,
            "last_error": self.last_error,
            "send_enabled": self.account.send_enabled,
            "sessions": len(self.list_sessions()),
        }

    def _refresh_health(self) -> None:
        status_fn = getattr(self._client, "status", None)
        browser = status_fn() if callable(status_fn) else {}
        browser_error = str(browser.get("last_error") or "")
        self.last_error = (self._local_error or browser_error)[:500]
        self.healthy = bool(
            browser.get("receive_connected", False)
            and browser.get("credentials_valid", False)
            and browser.get("identity_verified", False)
            and not self.last_error
        )

    def list_sessions(self) -> list[dict]:
        with self._state_lock:
            rows = []
            for contact_id, name in self._contacts.items():
                history = self._history.get(contact_id, [])
                latest = history[-1] if history else {}
                preview = str(latest.get("text") or self._previews.get(contact_id) or "")
                rows.append({
                    "wxid": contact_id,
                    "name": name or contact_id,
                    "summary": preview,
                    "last": preview,
                    "last_time": int(latest.get("ts") or 0),
                    "ts": int(latest.get("ts") or 0),
                    "unread": False,
                    "is_group": False,
                    "source_type": "douyin_private_message",
                    "avatar_url": self._avatars.get(contact_id, ""),
                })
            rows.sort(key=lambda row: row["last_time"], reverse=True)
            return rows

    def read_conversation(self, contact_id: str, limit: int = 80) -> list[dict]:
        with self._state_lock:
            target = dict(self._targets.get(contact_id) or {})
        if target:
            remote = None
            try:
                remote = self._client.read_conversation(target)
            except Exception:
                pass
            if remote is not None:
                self._replace_remote_history(contact_id, remote)
        with self._state_lock:
            return [dict(item) for item in self._history.get(contact_id, [])[-limit:]]

    def _replace_remote_history(self, contact_id: str, remote: list[dict]) -> None:
        with self._state_lock:
            existing = self._history.get(contact_id, [])
            existing_by_id = {
                str(row.get("local_id") or ""): row for row in existing
                if str(row.get("local_id") or "").startswith("douyin:")
            }
            snapshot_rows: list[dict] = []
            for item in remote:
                if not isinstance(item, dict):
                    continue
                message_id = str(item.get("message_id") or "").strip()
                if not message_id:
                    continue
                local_id = f"douyin:{message_id}"
                is_self = bool(item.get("is_self"))
                delivery_ts = int(item.get("ts") or 0) if is_self else 0
                row = {
                    "local_id": local_id,
                    "kind": str(item.get("kind") or "text"),
                    "text": str(item.get("text") or ""),
                    "sender_id": self.self_wxid() if is_self else contact_id,
                    "sender_name": (
                        "我" if is_self else str(
                            item.get("sender_name")
                            or self._contacts.get(contact_id, contact_id)
                        )
                    ),
                    "sender_avatar": str(item.get("sender_avatar") or ""),
                    "is_self": is_self,
                    "ts": delivery_ts,
                    "provenance": "reconciled" if is_self else "customer",
                    "source_type": "douyin_private_message",
                }
                for key in (
                    "media_url", "title", "description", "url", "display_time",
                    "video_url", "thumb_path", "media_path", "sequence",
                ):
                    value = item.get(key)
                    if key in {"url", "video_url"}:
                        value = self._safe_content_url(value)
                    if value or (key == "sequence" and value == 0):
                        row[key] = value
                previous = existing_by_id.get(local_id)
                if previous:
                    for key in (
                        "media_url", "title", "description", "url", "video_url",
                        "display_time", "sender_avatar",
                    ):
                        if key == "media_url" and row.get("kind") == "system":
                            continue
                        previous_value = previous.get(key)
                        if key in {"url", "video_url"}:
                            previous_value = self._safe_content_url(previous_value)
                        if not row.get(key) and previous_value:
                            row[key] = previous_value
                    placeholders = {"[视频]", "[分享内容]", "[图片]", "[表情]"}
                    if (
                        str(row.get("text") or "") in placeholders
                        and str(previous.get("text") or "") not in placeholders
                    ):
                        row["text"] = previous["text"]
                snapshot_rows.append(row)

            # The rendered Douyin DOM is the authority for browser-derived rows. Replacing
            # the previous snapshot removes stale parser output and keeps its visual order.
            # Locally-created rows are retained only when no rendered counterpart exists.
            preserved = []
            for row in existing:
                if str(row.get("local_id") or "").startswith("douyin:"):
                    continue
                duplicate = any(
                    bool(row.get("is_self")) == bool(remote_row.get("is_self"))
                    and str(row.get("text") or "") == str(remote_row.get("text") or "")
                    for remote_row in snapshot_rows
                )
                if not duplicate:
                    preserved.append(dict(row))
            next_rows = (snapshot_rows + preserved)[-self._HISTORY_LIMIT :]
            if next_rows != existing:
                self._history[contact_id] = next_rows
                self._save_state_locked()

    def reconcile_delivery(
        self, contact_id: str, text: str, since_ts: int,
    ) -> Literal["delivered", "not_delivered", "unknown"]:
        """Read the verified web history before an expired lease is ever retried."""
        threshold = max(0, int(since_ts or 0) - 5)
        with self._state_lock:
            target = dict(self._targets.get(contact_id) or {})
            cached_history = [dict(row) for row in self._history.get(contact_id, [])]
        if not target:
            return "unknown"
        # Rows whose IDs start with ``douyin:`` came from a rendered DOM snapshot;
        # unlike ``local:`` rows, they are authoritative even when the browser
        # command queue is temporarily occupied by a periodic scan.
        if any(
            str(message.get("local_id") or "").startswith("douyin:")
            and bool(message.get("is_self"))
            and str(message.get("text") or "") == str(text)
            and int(message.get("ts") or 0) >= threshold
            for message in cached_history
            if isinstance(message, dict)
        ):
            return "delivered"
        try:
            messages = self._client.read_conversation(target)
        except Exception:
            return "unknown"
        if any(
            bool(message.get("is_self"))
            and str(message.get("text") or "") == str(text)
            and int(message.get("ts") or 0) >= threshold
            for message in messages
            if isinstance(message, dict)
        ):
            return "delivered"
        return "not_delivered"

    def display_names(self, contact_ids: list[str]) -> dict[str, str]:
        with self._state_lock:
            return {
                contact_id: self._contacts.get(contact_id, contact_id)
                for contact_id in contact_ids
            }

    def _record_message_locked(
        self,
        message: InboundMsg,
        *,
        is_self: bool,
        provenance: str,
    ) -> None:
        contact_id = message["contact_id"]
        row = {
            "local_id": message["msg_id"],
            "kind": str(message.get("kind") or "text"),
            "text": message["text"],
            "sender_id": self.self_wxid() if is_self else message["sender_id"],
            "sender_name": "我" if is_self else str(message.get("sender_name") or contact_id),
            "sender_avatar": str(message.get("sender_avatar") or ""),
            "is_self": is_self,
            "ts": int(message.get("timestamp") or self._clock()),
            "provenance": provenance,
            "source_type": "douyin_private_message",
        }
        for key in (
            "media_url", "title", "description", "url", "display_time",
            "video_url", "thumb_path", "media_path", "sequence",
        ):
            value = message.get(key)
            if value:
                row[key] = value
        rows = self._history.setdefault(contact_id, [])
        local_id = str(row["local_id"])
        if any(str(existing.get("local_id") or "") == local_id for existing in rows):
            return
        rows.append(row)
        del rows[:-self._HISTORY_LIMIT]

    def _mark_processed_locked(self, conversation_id: str, message_key: str) -> None:
        self._seen_by_conversation.setdefault(conversation_id, set()).add(message_key)

    def _load_state(self) -> None:
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(raw, dict):
            return
        self._seen_by_conversation = {
            str(key): {str(value) for value in values if value}
            for key, values in dict(raw.get("seen_by_conversation") or {}).items()
            if isinstance(values, list)
        }
        self._legacy_processed = {
            str(value) for value in raw.get("processed", []) if value
        }
        self._targets = {
            str(key): self._safe_target(value)
            for key, value in dict(raw.get("targets") or {}).items()
            if isinstance(value, dict)
        }
        self._contacts = {
            str(key): str(value)
            for key, value in dict(raw.get("contacts") or {}).items()
        }
        self._previews = {
            str(key): str(value)
            for key, value in dict(raw.get("previews") or {}).items()
        }
        self._avatars = {
            str(key): str(value)
            for key, value in dict(raw.get("avatars") or {}).items()
        }
        self._history = {
            str(key): [dict(row) for row in value[-self._HISTORY_LIMIT :] if isinstance(row, dict)]
            for key, value in dict(raw.get("history") or {}).items()
            if isinstance(value, list)
        }
        self._pending_events = {
            str(key): dict(value)
            for key, value in dict(raw.get("pending_events") or {}).items()
            if isinstance(value, dict)
        }
        # `process_existing_messages=false` is a first-connection safety baseline,
        # not a license to discard messages that arrived while an established
        # account was restarting. A durable seen/pending set proves that baseline
        # already existed before this process started.
        self._had_persisted_baseline = bool(
            self._seen_by_conversation
            or self._legacy_processed
            or self._pending_events
        )
        # State v1 kept only a global bounded LRU. Recover stable IDs once while
        # upgrading, but never recreate an ID deliberately removed from v2 state
        # for a targeted replay/recovery.
        try:
            state_version = int(raw.get("version") or 1)
        except (TypeError, ValueError):
            state_version = 1
        if state_version < 2:
            for contact_id, rows in self._history.items():
                conversation_id = contact_id.removeprefix("im:")
                seen = self._seen_by_conversation.setdefault(conversation_id, set())
                for row in rows:
                    local_id = str(row.get("local_id") or "")
                    if (
                        local_id.startswith("douyin:")
                        and not row.get("is_self")
                        and local_id not in self._pending_events
                    ):
                        seen.add(local_id.removeprefix("douyin:"))

    def _save_state(self) -> None:
        with self._state_lock:
            self._save_state_locked()

    def _save_state_locked(self) -> None:
        payload = {
            "version": 2,
            "seen_by_conversation": {
                key: sorted(values) for key, values in self._seen_by_conversation.items()
            },
            "processed": sorted(self._legacy_processed),
            "targets": self._targets,
            "contacts": self._contacts,
            "previews": self._previews,
            "avatars": self._avatars,
            "history": self._history,
            "pending_events": self._pending_events,
        }
        self._atomic_write_json(self._state_path, payload)

    @staticmethod
    def _atomic_write_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(f"{path.suffix}.tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
