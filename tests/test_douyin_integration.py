from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.channels.douyin.browser_client import (
    DouyinBrowserError,
    DouyinBrowserImClient,
    DouyinConversation,
    DouyinConversationHistory,
    DouyinDeliveryReceipt,
    DouyinInboundEvent,
    _Command,
)
from app.channels.douyin.config import DouyinAccount, DouyinConfigError, load_douyin_accounts
from widget.douyin.adapter import DouyinAdapter
from widget.config import WidgetConfig
from widget.bridge_pool import BridgePool
from widget.models import SendResult
from widget.sender import RateLimiter, Sender
from run_widget_headless import handle_message


class FakeBrowserClient:
    def __init__(self) -> None:
        self.callback = None
        self.receipt = DouyinDeliveryReceipt("out-1", "confirmed")
        self.sent: list[tuple[dict, str, bool, bool]] = []
        self.scan_count = 0
        self.running = False
        self.remote_messages: list[dict] = []

    def start(self, callback, conversation_callback=None, history_callback=None) -> None:
        self.callback = callback
        self.conversation_callback = conversation_callback
        self.history_callback = history_callback
        self.running = True

    def stop(self) -> None:
        self.running = False

    def status(self) -> dict:
        return {
            "credentials_valid": self.running,
            "identity_fingerprint": "0123456789abcdef",
            "identity_verified": self.running,
            "receive_connected": self.running,
            "last_error": "",
            "last_scan_at": 123,
        }

    def emit(self, event: DouyinInboundEvent) -> None:
        assert self.callback is not None
        self.callback(event)

    def send_text(
        self,
        target: dict,
        text: str,
        *,
        allow_dom_target: bool = False,
        allow_dom_name_fallback: bool = False,
    ) -> DouyinDeliveryReceipt:
        self.sent.append((target, text, allow_dom_target, allow_dom_name_fallback))
        return self.receipt

    def scan_now(self) -> int:
        self.scan_count += 1
        return 2

    def read_conversation(self, _target: dict) -> list[dict]:
        return [dict(row) for row in self.remote_messages]


def _account(tmp_path, **changes) -> DouyinAccount:
    account = DouyinAccount(
        account_id="personal_a",
        display_name="个人号 A",
        tenant_id=1,
        login="admin",
        data_dir=str(tmp_path / "state"),
        profile_dir=str(tmp_path / "profile"),
    )
    return replace(account, **changes)


def _event(key: str, *, initial: bool = False, text: str = "什么时候发货") -> DouyinInboundEvent:
    return DouyinInboundEvent(
        message_key=key,
        conversation_id="conversation-1",
        sender_id="user-1",
        sender_name="Alice",
        text=text,
        timestamp=1_700_000_000,
        target={"uid": "user-1", "name": "Alice", "index": 0},
        initial_scan=initial,
    )


def test_config_loader_rejects_unsafe_url_and_duplicate_ids(tmp_path) -> None:
    config = tmp_path / "accounts.yaml"
    config.write_text(
        """accounts:
  - account_id: a
    display_name: A
    tenant_id: 1
    login: admin
    messages_url: http://example.com/
""",
        encoding="utf-8",
    )
    with pytest.raises(DouyinConfigError, match="HTTPS"):
        load_douyin_accounts(config)

    config.write_text(
        """accounts:
  - {account_id: a, display_name: A, tenant_id: 1, login: admin}
  - {account_id: a, display_name: B, tenant_id: 1, login: admin}
""",
        encoding="utf-8",
    )
    with pytest.raises(DouyinConfigError, match="重复"):
        load_douyin_accounts(config)

    config.write_text(
        """accounts:
  - account_id: a
    display_name: A
    tenant_id: 1
    login: admin
    password: plaintext
""",
        encoding="utf-8",
    )
    with pytest.raises(DouyinConfigError, match="不允许明文"):
        load_douyin_accounts(config)


def test_bridge_pool_inherits_global_credential_for_matching_account_identity() -> None:
    base = WidgetConfig(tenant_id=1, login="demo", password="global-password")
    pool = BridgePool(base, bridge_factory=lambda cfg: cfg)
    account = SimpleNamespace(
        tenant_id=1, login="demo", password="", password_enc=""
    )

    derived = pool.for_instance(account)

    assert derived.login == "demo"
    assert derived.password == "global-password"


def test_bridge_pool_rejects_different_login_without_its_own_credential() -> None:
    base = WidgetConfig(tenant_id=1, login="demo", password="global-password")
    pool = BridgePool(base, bridge_factory=lambda cfg: cfg)
    account = SimpleNamespace(
        tenant_id=1, login="admin", password="", password_enc=""
    )

    with pytest.raises(ValueError, match="没有独立凭据"):
        pool.for_instance(account)


def test_initial_scan_builds_baseline_then_new_message_enters_pipeline(tmp_path) -> None:
    client = FakeBrowserClient()
    adapter = DouyinAdapter(_account(tmp_path), client)
    received = []
    ready = threading.Event()

    def on_message(message) -> None:
        received.append(message)
        ready.set()

    adapter.start(on_message)
    client.emit(_event("old", initial=True))
    assert not ready.wait(0.1)
    assert adapter.list_sessions()[0]["name"] == "Alice"

    client.emit(_event("new", text="能改地址吗"))
    assert ready.wait(1.0)
    assert received[0]["channel"] == "douyin#personal_a"
    assert received[0]["contact_id"] == "im:conversation-1"
    assert received[0]["source_type"] == "douyin_private_message"

    client.emit(_event("new", text="能改地址吗"))
    assert len(received) == 1
    adapter.stop()


def test_restart_initial_scan_delivers_messages_newer_than_persisted_baseline(tmp_path) -> None:
    account = _account(tmp_path)
    first_client = FakeBrowserClient()
    first = DouyinAdapter(account, first_client)
    first.start(lambda _message: None)
    first_client.emit(_event("known-before-restart", initial=True))
    time.sleep(0.1)
    first.stop()

    recovered_client = FakeBrowserClient()
    recovered = DouyinAdapter(account, recovered_client)
    received = []
    ready = threading.Event()

    def on_message(message) -> None:
        received.append(message)
        ready.set()

    recovered.start(on_message)
    recovered_client.emit(
        _event("arrived-during-restart", initial=True, text="你们几点营业")
    )

    assert ready.wait(1.0)
    assert received[0]["text"] == "你们几点营业"
    recovered.stop()


def test_send_requires_account_switch_and_confirmed_page_receipt(tmp_path) -> None:
    client = FakeBrowserClient()
    disabled = DouyinAdapter(_account(tmp_path), client)
    disabled._accept_event(_event("in-1"))
    result = disabled.send_message("im:conversation-1", "今天发货", provenance="ai")
    assert result.ok is False
    assert "send_enabled=false" in result.error
    assert client.sent == []

    enabled = DouyinAdapter(_account(tmp_path, send_enabled=True), client)
    enabled._accept_event(_event("in-2"))
    client.running = True
    result = enabled.send_message("im:conversation-1", "今天发货", provenance="ai")
    assert result.ok is True
    assert client.sent[-1][2] is False
    assert enabled.read_conversation("im:conversation-1")[-1]["is_self"] is True

    result = enabled.send_message("im:conversation-1", "人工确认", provenance="human")
    assert result.ok is True
    assert client.sent[-1][2] is False
    assert client.sent[-1][3] is False

    client.receipt = DouyinDeliveryReceipt(
        "out-2", "unknown", "页面没有出现可确认的本人消息"
    )
    result = enabled.send_message("im:conversation-1", "再次发送", provenance="ai")
    assert result.ok is False
    assert "没有出现" in result.error


def test_ai_send_uses_verified_dom_target_after_both_send_switches_allow_it(tmp_path) -> None:
    client = FakeBrowserClient()
    adapter = DouyinAdapter(_account(tmp_path, send_enabled=True), client)
    event = replace(
        _event("in-dom"),
        target={
            "uid": "dom:conversation-fingerprint",
            "name": "Alice",
            "index": 0,
            "locator_kind": "conversation_item",
            "list_scope": "inbox",
        },
    )
    adapter._accept_event(event)
    client.running = True

    result = adapter.send_message(
        "im:conversation-1", "本店每天上午9点到晚上21点营业。", provenance="ai"
    )

    assert result.ok is True
    assert client.sent[-1][2] is True
    assert client.sent[-1][3] is False

    result = adapter.send_message(
        "im:conversation-1", "人工确认", provenance="human"
    )
    assert result.ok is True
    assert client.sent[-1][2] is True
    assert client.sent[-1][3] is True


def test_adapter_state_recovery_preserves_dedup_and_target_without_credentials(tmp_path) -> None:
    account = _account(tmp_path, send_enabled=True)
    first = DouyinAdapter(account, FakeBrowserClient())
    first._accept_event(_event("stable-1"))

    client = FakeBrowserClient()
    recovered = DouyinAdapter(account, client)
    received = []
    recovered._on_message = received.append
    recovered._accept_event(_event("stable-1"))
    assert received == []
    assert recovered.display_names(["im:conversation-1"])["im:conversation-1"] == "Alice"
    assert "cookie" not in recovered._state_path.read_text(encoding="utf-8").casefold()

    client.running = True
    assert recovered.send_message("im:conversation-1", "收到").ok is True


def test_version_two_state_respects_an_intentionally_removed_seen_message(tmp_path) -> None:
    account = _account(tmp_path)
    first = DouyinAdapter(account, FakeBrowserClient())
    first._accept_event(_event("replay-one"))
    state_path = account.resolved_data_dir / "private_message_state.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["seen_by_conversation"]["conversation-1"].remove("replay-one")
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    recovered = DouyinAdapter(account, FakeBrowserClient())

    assert "replay-one" not in recovered._seen_by_conversation["conversation-1"]


def test_processed_ids_are_persistent_per_conversation_without_global_lru_eviction(tmp_path) -> None:
    account = _account(tmp_path)
    first = DouyinAdapter(account, FakeBrowserClient())
    with first._state_lock:
        first._mark_processed_locked("conversation-1", "old-stable")
        for index in range(4_500):
            first._mark_processed_locked("busy-conversation", f"message-{index}")
        first._save_state_locked()

    recovered = DouyinAdapter(account, FakeBrowserClient())
    received = []
    recovered._on_message = received.append
    recovered._accept_event(_event("old-stable"))

    assert received == []
    assert "old-stable" in recovered._seen_by_conversation["conversation-1"]


def test_failed_pipeline_callback_is_replayed_after_restart(tmp_path) -> None:
    account = _account(tmp_path)
    first_client = FakeBrowserClient()
    first = DouyinAdapter(account, first_client)
    failed = threading.Event()

    def fail(_message) -> None:
        failed.set()
        raise RuntimeError("backend unavailable")

    first.start(fail)
    first_client.emit(_event("retryable-1"))
    assert failed.wait(1.0)
    first.stop()

    recovered_client = FakeBrowserClient()
    recovered = DouyinAdapter(account, recovered_client)
    delivered = threading.Event()
    messages = []

    def succeed(message) -> None:
        messages.append(message)
        delivered.set()

    recovered.start(succeed)
    assert delivered.wait(1.0)
    assert messages[0]["msg_id"] == "douyin:retryable-1"
    recovered.stop()


def test_backend_error_outcome_is_persisted_for_retry_instead_of_consumed(tmp_path) -> None:
    adapter = DouyinAdapter(_account(tmp_path), FakeBrowserClient())
    adapter._on_message = lambda _message: "error"

    adapter._accept_event(_event("backend-error"))

    assert "backend-error" not in adapter._seen_by_conversation["conversation-1"]
    assert "douyin:backend-error" in adapter._pending_events


class FakeCookieContext:
    def __init__(self, cookies: list[dict]) -> None:
        self._cookies = cookies

    def cookies(self, _url: str) -> list[dict]:
        return list(self._cookies)


def test_browser_identity_must_match_bound_fingerprint(tmp_path) -> None:
    base = _account(tmp_path)
    context = FakeCookieContext([
        {"name": "sessionid", "value": "secret-session"},
        {"name": "uid_tt", "value": "stable-account-material"},
    ])
    probe = DouyinBrowserImClient(base)
    assert probe._refresh_identity(context) is False
    fingerprint = probe.status()["identity_fingerprint"]
    assert len(fingerprint) == 16
    assert "尚未绑定" in probe.status()["last_error"]

    matched = DouyinBrowserImClient(
        replace(base, expected_identity_fingerprint=fingerprint)
    )
    assert matched._refresh_identity(context) is True
    assert matched.status()["identity_verified"] is True

    mismatch = DouyinBrowserImClient(
        replace(base, expected_identity_fingerprint="0" * 16)
    )
    assert mismatch._refresh_identity(context) is False
    assert "不一致" in mismatch.status()["last_error"]


def test_scan_emits_every_stable_incoming_message_and_uses_preview_changes(tmp_path) -> None:
    client = DouyinBrowserImClient(_account(tmp_path))
    events = []
    client._on_event = events.append
    client._open_target = lambda _page, _target, **_kwargs: None

    class Page:
        preview = "两条新消息"

        def evaluate(self, script):
            if "role=\"listitem\"" in script:
                return [{"uid": "user-1", "name": "Alice", "preview": self.preview,
                         "unread": True}]
            return {"title": "Alice", "messages": [
                {"stable": "m-1", "text": "第一条", "own": False},
                {"stable": "m-2", "text": "第二条", "own": False},
                {"stable": "m-self", "text": "旧回复", "own": True},
            ]}

    page = Page()
    assert client._scan(page, initial_scan=True) == 2
    assert [event.message_key for event in events] == ["m-1", "m-2"]

    events.clear()
    page.preview = "两条新消息"
    page.evaluate = lambda script: (
        [{"uid": "user-1", "name": "Alice", "preview": page.preview, "unread": False}]
        if "role=\"listitem\"" in script else {}
    )
    assert client._scan(page, initial_scan=False) == 0


def test_scan_opens_douyin_inbox_before_reading_conversations(tmp_path) -> None:
    client = DouyinBrowserImClient(_account(tmp_path))
    client._on_event = lambda _event: None
    client._open_target = lambda _page, _target: None

    class Page:
        opened = False
        waited = []

        def evaluate(self, script):
            if "data-acs-douyin-inbox-target" in script:
                return "target"
            if "role=\"listitem\"" in script:
                if not self.opened:
                    return []
                return [{
                    "uid": "user-1", "name": "Alice", "preview": "你好", "unread": False,
                }]
            return {"title": "Alice", "messages": []}

        def locator(self, _selector):
            page = self

            class Target:
                @property
                def first(self):
                    return self

                def count(self):
                    return 1

                def click(self):
                    page.opened = True

            return Target()

        def wait_for_timeout(self, milliseconds):
            self.waited.append(milliseconds)

    page = Page()
    assert client._scan(page, initial_scan=True) == 0
    assert page.opened is True
    assert page.waited == [900]


def test_scan_waits_for_animated_inbox_conversation_list(tmp_path) -> None:
    client = DouyinBrowserImClient(_account(tmp_path))
    client._on_event = lambda _event: None
    client._open_target = lambda _page, _target: None

    class Page:
        opened = False
        elapsed_ms = 0

        def evaluate(self, script):
            if "data-acs-douyin-inbox-target" in script:
                return "target"
            if "role=\"listitem\"" in script:
                if not self.opened or self.elapsed_ms < 1_500:
                    return []
                return [{
                    "uid": "user-1", "name": "Alice", "preview": "你好", "unread": False,
                }]
            return {"title": "Alice", "messages": []}

        def locator(self, _selector):
            page = self

            class Target:
                @property
                def first(self):
                    return self

                def count(self):
                    return 1

                def click(self):
                    page.opened = True

            return Target()

        def wait_for_timeout(self, milliseconds):
            self.elapsed_ms += milliseconds

    page = Page()
    assert client._scan(page, initial_scan=True) == 0
    assert page.opened is True
    assert page.elapsed_ms >= 1_500


def test_current_douyin_dom_conversation_is_discovered_without_platform_uid(tmp_path) -> None:
    client = DouyinBrowserImClient(_account(tmp_path))
    conversations = []
    client._on_event = lambda _event: None
    client._on_conversation = conversations.append
    client._open_target = lambda _page, _target: None

    class Page:
        def evaluate(self, script):
            if "conversation-item" in script and "identity_seed" in script:
                return [{
                    "uid": "",
                    "identity_seed": "Alice\n/avatar/path",
                    "locator_kind": "conversation_item",
                    "index": 0,
                    "name": "Alice",
                    "preview": "你好",
                    "unread": False,
                }]
            return {"title": "Alice", "messages": []}

    assert client._scan(Page(), initial_scan=True) == 0
    assert len(conversations) == 1
    conversation = conversations[0]
    assert conversation.conversation_id.startswith("dom:")
    assert conversation.target["locator_kind"] == "conversation_item"
    assert conversation.name == "Alice"


def test_scan_registers_all_conversations_before_one_chat_read_fails(tmp_path) -> None:
    client = DouyinBrowserImClient(_account(tmp_path, process_existing_messages=True))
    conversations = []
    client._on_conversation = conversations.append
    client._open_target = lambda _page, _target: (_ for _ in ()).throw(
        RuntimeError("changed chat DOM")
    )

    class Page:
        def evaluate(self, script):
            if "conversation-item" in script and "identity_seed" in script:
                return [
                    {"uid": "user-1", "name": "Alice", "preview": "one", "unread": True},
                    {"uid": "user-2", "name": "Bob", "preview": "two", "unread": True},
                ]
            return {}

    assert client._scan(Page(), initial_scan=True) == 0
    assert [row.conversation_id for row in conversations] == ["user-1", "user-2"]
    assert client.status()["last_error"] == "changed chat DOM"


def test_scan_discovers_and_reads_stranger_conversations(tmp_path) -> None:
    client = DouyinBrowserImClient(_account(tmp_path, process_existing_messages=True))
    conversations = []
    histories = []
    events = []
    opened_targets = []
    client._on_conversation = conversations.append
    client._on_history = histories.append
    client._on_event = events.append
    client._open_target = lambda _page, target: opened_targets.append(dict(target))

    class Page:
        state = "main"
        stranger_message_version = 1

        def evaluate(self, script):
            if "identity_seed" in script and "data-acs-douyin-active-index" in script:
                if self.state == "main":
                    return [{
                        "identity_seed": "Alice\n/avatar/alice",
                        "locator_kind": "conversation_item",
                        "index": 0,
                        "name": "Alice",
                        "preview": "普通会话",
                        "unread": False,
                    }]
                return [{
                    "identity_seed": "Visitor\n/avatar/visitor",
                    "locator_kind": "conversation_item",
                    "index": 0,
                    "name": "Visitor",
                    "preview": f"陌生人消息 {self.stranger_message_version}",
                    "unread": True,
                }]
            if "data-acs-douyin-stranger-entry" in script:
                return self.state == "main"
            if (
                "conversationStrangerBoxwrapper" in script
                and "const strangerHeaders" in script
            ):
                return self.state == "main"
            if (
                "conversationStrangerConversationListhead" in script
                and ")).some(el =>" in script
            ):
                return self.state == "stranger"
            if "data-acs-douyin-visible-back" in script:
                return self.state == "stranger"
            if "messages: messages.slice(-200)" in script:
                target = opened_targets[-1]
                versions = (
                    range(1, self.stranger_message_version + 1)
                    if target["list_scope"] == "stranger"
                    else range(1, 2)
                )
                return {
                    "title": target["name"],
                    "messages": [
                        {
                            "stable": f"message-{target['list_scope']}-{version}",
                            "text": f"来自 {target['list_scope']} {version}",
                            "own": False,
                        }
                        for version in versions
                    ],
                }
            return False

        def wait_for_timeout(self, _milliseconds):
            return None

        def locator(self, selector):
            page = self

            class Target:
                @property
                def first(self):
                    return self

                def count(self):
                    return 1

                def nth(self, _index):
                    return self

                def scroll_into_view_if_needed(self, **_kwargs):
                    return None

                def click(self, **_kwargs):
                    if "stranger-entry" in selector:
                        page.state = "stranger"
                    elif "visible-back" in selector:
                        raise AssertionError("animated back buttons must not use actionability click")
                    return None

                def dispatch_event(self, event):
                    assert event == "click"
                    if "visible-back" in selector:
                        page.state = "main"
                    return None

            return Target()

    page = Page()
    assert client._scan(page, initial_scan=True) == 2
    assert [row.target["list_scope"] for row in conversations] == ["inbox", "stranger"]
    assert [row.conversation_id for row in histories] == [
        conversations[0].conversation_id,
        conversations[1].conversation_id,
    ]
    assert [target["list_scope"] for target in opened_targets] == ["inbox", "stranger"]
    assert len(events) == 2
    assert events[1].target["list_scope"] == "stranger"

    events.clear()
    page.stranger_message_version = 2
    assert client._scan(page, initial_scan=False) >= 1
    new_event = next(event for event in events if event.text == "来自 stranger 2")
    assert new_event.initial_scan is False
    assert new_event.target["list_scope"] == "stranger"


def test_stranger_scope_wins_when_same_conversation_is_visible_in_both_lists(tmp_path) -> None:
    client = DouyinBrowserImClient(_account(tmp_path))
    conversations = []
    client._on_conversation = conversations.append
    client._open_target = lambda _page, _target: None
    client._return_to_main_inbox = lambda _page: True
    client._open_stranger_list = lambda _page: True

    class Page:
        scan_calls = 0

        def evaluate(self, script):
            if "identity_seed" in script and "data-acs-douyin-active-index" in script:
                self.scan_calls += 1
                return [{
                    "identity_seed": "Visitor\n/avatar/visitor",
                    "locator_kind": "conversation_item",
                    "index": 0,
                    "name": "Visitor",
                    "preview": "same conversation",
                    "unread": self.scan_calls == 2,
                }]
            if "messages: messages.slice(-200)" in script:
                return {"title": "Visitor", "messages": []}
            return False

    assert client._scan(Page(), initial_scan=True) == 0
    assert len(conversations) == 1
    assert conversations[0].target["list_scope"] == "stranger"


class _VirtualConversationPage:
    def __init__(
        self, pages: list[list[dict]], *, rendered_text: dict[str, str] | None = None
    ) -> None:
        self.pages = pages
        self.position = 0
        self.clicked = ""
        self.rendered_text = rendered_text or {}

    def evaluate(self, script, arg=None):
        if "identity_seed" in script and "data-acs-douyin-active-index" in script:
            return [dict(row, index=index) for index, row in enumerate(self.pages[self.position])]
        if "const desired = action === 'top'" in script:
            before = self.position
            if arg == "top":
                self.position = 0
            elif self.position < len(self.pages) - 1:
                self.position += 1
            return {
                "found": True,
                "moved": self.position != before,
                "at_end": self.position == len(self.pages) - 1,
            }
        if arg is not None:
            return self.clicked == arg
        return False

    def wait_for_timeout(self, _milliseconds):
        return None

    def locator(self, selector):
        page = self

        class Candidates:
            def count(self):
                return len(page.pages[page.position])

            def nth(self, index):
                row = page.pages[page.position][index]

                class Candidate:
                    def inner_text(self):
                        return page.rendered_text.get(
                            str(row["uid"]), str(row["name"])
                        )

                    def scroll_into_view_if_needed(self):
                        return None

                    def click(self):
                        page.clicked = str(row["name"])

                return Candidate()

        assert selector == '[data-acs-douyin-active-index]'
        return Candidates()


def test_open_target_traverses_virtualized_conversation_list(monkeypatch) -> None:
    monkeypatch.setattr(
        DouyinBrowserImClient, "_return_to_main_inbox", staticmethod(lambda _page: True)
    )
    page = _VirtualConversationPage([
        [
            {"uid": "dom:a", "name": "Alice", "locator_kind": "conversation_item"},
            {"uid": "dom:b", "name": "Bob", "locator_kind": "conversation_item"},
        ],
        [{"uid": "dom:target", "name": "Visitor", "locator_kind": "conversation_item"}],
    ])

    DouyinBrowserImClient._open_target(page, {
        "uid": "dom:target",
        "name": "Visitor",
        "locator_kind": "conversation_item",
        "list_scope": "inbox",
    })

    assert page.position == 1
    assert page.clicked == "Visitor"


def test_open_target_uses_scanned_title_when_row_text_order_changes(monkeypatch) -> None:
    monkeypatch.setattr(
        DouyinBrowserImClient, "_return_to_main_inbox", staticmethod(lambda _page: True)
    )
    page = _VirtualConversationPage(
        [[{"uid": "dom:target", "name": "Visitor", "locator_kind": "conversation_item"}]],
        rendered_text={"dom:target": "昨天\nVisitor\n你好"},
    )

    DouyinBrowserImClient._open_target(page, {
        "uid": "dom:target",
        "name": "Visitor",
        "locator_kind": "conversation_item",
        "list_scope": "inbox",
    })

    assert page.clicked == "Visitor"


def test_open_target_rejects_duplicate_name_after_full_virtual_traversal(monkeypatch) -> None:
    monkeypatch.setattr(
        DouyinBrowserImClient, "_return_to_main_inbox", staticmethod(lambda _page: True)
    )
    page = _VirtualConversationPage([
        [{"uid": "dom:new-a", "name": "Same", "locator_kind": "conversation_item"}],
        [{"uid": "dom:new-b", "name": "Same", "locator_kind": "conversation_item"}],
    ])

    with pytest.raises(DouyinBrowserError, match="唯一匹配"):
        DouyinBrowserImClient._open_target(page, {
            "uid": "dom:stale",
            "name": "Same",
            "locator_kind": "conversation_item",
            "list_scope": "inbox",
        })
    assert page.clicked == ""


def test_open_target_disallows_unique_name_fallback_for_automated_send(monkeypatch) -> None:
    monkeypatch.setattr(
        DouyinBrowserImClient, "_return_to_main_inbox", staticmethod(lambda _page: True)
    )
    page = _VirtualConversationPage([
        [{"uid": "dom:rotated", "name": "Visitor", "locator_kind": "conversation_item"}],
    ])

    with pytest.raises(DouyinBrowserError, match="唯一匹配"):
        DouyinBrowserImClient._open_target(
            page,
            {
                "uid": "dom:stale",
                "name": "Visitor",
                "locator_kind": "conversation_item",
                "list_scope": "inbox",
            },
            allow_dom_name_fallback=False,
        )
    assert page.clicked == ""

    DouyinBrowserImClient._open_target(
        page,
        {
            "uid": "dom:stale",
            "name": "Visitor",
            "locator_kind": "conversation_item",
            "list_scope": "inbox",
        },
        allow_dom_name_fallback=True,
    )
    assert page.clicked == "Visitor"


def test_discovered_conversation_appears_in_adapter_sessions(tmp_path) -> None:
    browser = FakeBrowserClient()
    adapter = DouyinAdapter(_account(tmp_path), browser)
    adapter.start(lambda _message: None)
    try:
        assert browser.conversation_callback is not None
        browser.conversation_callback(DouyinConversation(
            conversation_id="dom:abc",
            name="Alice",
            preview="你好",
            target={
                "uid": "dom:abc",
                "name": "Alice",
                "index": 2,
                "locator_kind": "conversation_item",
            },
        ))
        assert adapter.list_sessions() == [{
            "wxid": "im:dom:abc",
            "name": "Alice",
            "summary": "你好",
            "last": "你好",
            "last_time": 0,
            "ts": 0,
            "unread": False,
            "is_group": False,
            "source_type": "douyin_private_message",
            "avatar_url": "",
        }]
    finally:
        adapter.stop()


def test_dirty_legacy_dom_session_migrates_to_clean_name_and_avatar(tmp_path) -> None:
    adapter = DouyinAdapter(_account(tmp_path), FakeBrowserClient())
    old_contact = "im:dom:old"
    with adapter._state_lock:
        adapter._targets[old_contact] = {
            "uid": "dom:old", "name": "Alice\n昨天", "index": 2,
            "locator_kind": "conversation_item",
        }
        adapter._contacts[old_contact] = "Alice\n昨天"
        adapter._previews[old_contact] = "旧预览"
        adapter._history[old_contact] = [{"local_id": "douyin:m-1", "text": "你好"}]
        adapter._seen_by_conversation["dom:old"] = {"m-1"}

    adapter._upsert_conversation(DouyinConversation(
        conversation_id="dom:new",
        name="Alice",
        preview="新预览",
        avatar_url="https://p3.douyinpic.com/avatar.jpg",
        target={
            "uid": "dom:new", "name": "Alice", "index": 2,
            "locator_kind": "conversation_item",
        },
    ))

    assert old_contact not in adapter._contacts
    assert adapter._history["im:dom:new"][0]["local_id"] == "douyin:m-1"
    assert adapter._seen_by_conversation["dom:new"] == {"m-1"}
    session = adapter.list_sessions()[0]
    assert session["name"] == "Alice"
    assert session["avatar_url"].endswith("avatar.jpg")


def test_browser_read_preserves_structured_media_and_sender_avatar(tmp_path) -> None:
    client = DouyinBrowserImClient(_account(tmp_path))
    client._open_target = lambda _page, _target, **_kwargs: None

    class Page:
        def evaluate(self, _script):
            return {"messages": [{
                "stable": "media-1",
                "text": "[图片]",
                "kind": "image",
                "media_url": "https://p3.douyinpic.com/photo.jpg",
                "sender_name": "Alice",
                "sender_avatar": "https://p3.douyinpic.com/avatar.jpg",
                "description": "图片说明",
                "display_time": "昨天 12:00",
                "url": "https://www.douyin.com/video/7674274143898127081",
                "video_url": "https://www.douyin.com/video/7674274143898127081",
                "own": False,
            }, {
                "stable": "self-1",
                "text": "收到",
                "kind": "text",
                "own": True,
            }]}

    rows = client._read(Page(), {
        "uid": "dom:abc", "name": "Alice", "index": 0,
        "locator_kind": "conversation_item",
    })
    assert len(rows) == 2
    assert rows[0]["kind"] == "image"
    assert rows[0]["media_url"].endswith("photo.jpg")
    assert rows[0]["sender_avatar"].endswith("avatar.jpg")
    assert rows[0]["description"] == "图片说明"
    assert rows[0]["display_time"] == "昨天 12:00"
    assert rows[0]["sequence"] == 0
    assert rows[0]["url"].endswith("/video/7674274143898127081")
    assert rows[0]["video_url"].endswith("/video/7674274143898127081")
    assert rows[0]["message_id"] != "media-1"
    assert rows[1]["is_self"] is True


def test_adapter_keeps_images_and_unverified_historical_self_messages(tmp_path) -> None:
    browser = FakeBrowserClient()
    adapter = DouyinAdapter(_account(tmp_path), browser)
    adapter._upsert_conversation(DouyinConversation(
        conversation_id="dom:abc",
        name="Alice",
        preview="[图片]",
        target={
            "uid": "dom:abc", "name": "Alice", "index": 0,
            "locator_kind": "conversation_item",
        },
    ))
    browser.remote_messages = [{
        "message_id": "image-1", "text": "[图片]", "kind": "image",
        "media_url": "https://p3.douyinpic.com/photo.jpg",
        "sender_name": "Alice",
        "sender_avatar": "https://p3.douyinpic.com/avatar.jpg",
        "is_self": False, "ts": 0,
    }, {
        "message_id": "self-1", "text": "收到", "kind": "text",
        "is_self": True, "ts": 0,
    }]

    rows = adapter.read_conversation("im:dom:abc")
    assert len(rows) == 2
    assert rows[0]["kind"] == "image"
    assert rows[0]["media_url"].endswith("photo.jpg")
    assert rows[0]["sender_avatar"].endswith("avatar.jpg")
    assert rows[1]["is_self"] is True


def test_background_history_snapshot_populates_ui_without_ai_dispatch(tmp_path) -> None:
    browser = FakeBrowserClient()
    adapter = DouyinAdapter(_account(tmp_path), browser)
    dispatched = []
    adapter.start(dispatched.append)
    try:
        assert browser.history_callback is not None
        browser.history_callback(DouyinConversationHistory(
            conversation_id="dom:abc",
            messages=[{
                "message_id": "self-only-1",
                "text": "已收到",
                "kind": "text",
                "is_self": True,
                "ts": 0,
            }],
        ))
        rows = adapter.read_conversation("im:dom:abc")
        assert len(rows) == 1
        assert rows[0]["is_self"] is True
        assert dispatched == []
    finally:
        adapter.stop()


def test_verified_browser_snapshot_replaces_stale_rows_and_preserves_visual_order(tmp_path) -> None:
    browser = FakeBrowserClient()
    adapter = DouyinAdapter(_account(tmp_path), browser)
    adapter._upsert_conversation(DouyinConversation(
        conversation_id="dom:abc",
        name="Alice",
        preview="newest",
        target={
            "uid": "dom:abc", "name": "Alice", "index": 0,
            "locator_kind": "conversation_item",
        },
    ))
    adapter._history["im:dom:abc"] = [{
        "local_id": "douyin:stale", "text": "旧的错误解析", "is_self": False,
    }]
    browser.remote_messages = [{
        "message_id": "visual-first", "text": "第一条", "kind": "text",
        "is_self": False, "ts": 0, "sequence": 0,
    }, {
        "message_id": "visual-second", "text": "第二条", "kind": "text",
        "is_self": True, "ts": 0, "sequence": 1,
    }]

    rows = adapter.read_conversation("im:dom:abc")

    assert [row["text"] for row in rows] == ["第一条", "第二条"]
    assert [row["sequence"] for row in rows] == [0, 1]
    assert all(row["local_id"] != "douyin:stale" for row in rows)


def test_verified_empty_snapshot_clears_stale_browser_rows(tmp_path) -> None:
    adapter = DouyinAdapter(_account(tmp_path), FakeBrowserClient())
    adapter._history["im:dom:abc"] = [{
        "local_id": "douyin:stale", "text": "不存在的消息", "is_self": False,
    }]

    adapter._upsert_history_snapshot(DouyinConversationHistory("dom:abc", []))

    assert adapter._history["im:dom:abc"] == []


def test_refresh_does_not_downgrade_lazy_loaded_video_metadata(tmp_path) -> None:
    adapter = DouyinAdapter(_account(tmp_path), FakeBrowserClient())
    adapter._history["im:dom:abc"] = [{
        "local_id": "douyin:video-1",
        "kind": "video",
        "text": "视频作者",
        "title": "视频作者",
        "media_url": "https://p3.douyinpic.com/video-cover.jpg",
        "sender_avatar": "https://p3.douyinpic.com/avatar.jpg",
        "is_self": False,
    }]

    adapter._upsert_history_snapshot(DouyinConversationHistory("dom:abc", [{
        "message_id": "video-1",
        "kind": "video",
        "text": "[视频]",
        "is_self": False,
        "ts": 0,
        "sequence": 0,
    }]))

    row = adapter._history["im:dom:abc"][0]
    assert row["text"] == "视频作者"
    assert row["title"] == "视频作者"
    assert row["media_url"].endswith("video-cover.jpg")
    assert row["sender_avatar"].endswith("avatar.jpg")


def test_system_greeting_does_not_keep_suggested_sticker_as_message_media(tmp_path) -> None:
    adapter = DouyinAdapter(_account(tmp_path), FakeBrowserClient())
    adapter._history["im:dom:abc"] = [{
        "local_id": "douyin:greeting-1",
        "kind": "system",
        "text": "我们已成为朋友，可以开始聊天了",
        "media_url": "https://p3.douyinpic.com/suggested-sticker.jpg",
        "is_self": False,
    }]

    adapter._upsert_history_snapshot(DouyinConversationHistory("dom:abc", [{
        "message_id": "greeting-1",
        "kind": "system",
        "text": "我们已成为朋友，可以开始聊天了",
        "is_self": False,
        "ts": 0,
        "sequence": 0,
    }]))

    assert not adapter._history["im:dom:abc"][0].get("media_url")


def test_homepage_placeholder_is_removed_but_video_detail_url_is_kept(tmp_path) -> None:
    adapter = DouyinAdapter(_account(tmp_path), FakeBrowserClient())
    adapter._history["im:dom:abc"] = [{
        "local_id": "douyin:card-1",
        "kind": "app_post",
        "text": "分享内容",
        "url": "https://www.douyin.com/",
        "is_self": False,
    }]

    adapter._upsert_history_snapshot(DouyinConversationHistory("dom:abc", [{
        "message_id": "card-1",
        "kind": "app_post",
        "text": "分享内容",
        "is_self": False,
        "ts": 0,
        "sequence": 0,
    }, {
        "message_id": "video-1",
        "kind": "video",
        "text": "视频内容",
        "url": "https://www.douyin.com/video/7674274143898127081",
        "video_url": "https://www.douyin.com/video/7674274143898127081",
        "is_self": False,
        "ts": 0,
        "sequence": 1,
    }]))

    rows = adapter._history["im:dom:abc"]
    assert not rows[0].get("url")
    assert rows[1]["url"].endswith("/video/7674274143898127081")
    assert rows[1]["video_url"].endswith("/video/7674274143898127081")
    assert adapter._safe_content_url("https://example.com/video/123") == ""


def test_scan_round_robin_detects_same_preview_and_same_text_with_new_message_id(tmp_path) -> None:
    client = DouyinBrowserImClient(_account(tmp_path))
    events = []
    client._on_event = events.append
    client._open_target = lambda _page, _target: None
    payloads = iter([
        {"title": "Alice", "messages": [
            {"stable": "m-1", "text": "在吗", "own": False},
        ]},
        {"title": "Alice", "messages": [
            {"stable": "m-1", "text": "在吗", "own": False},
            {"stable": "m-2", "text": "在吗", "own": False},
        ]},
    ])

    class Page:
        def evaluate(self, script):
            if "role=\"listitem\"" in script:
                return [{"uid": "user-1", "name": "Alice", "preview": "在吗", "unread": False}]
            if (
                "conversationStrangerBoxwrapper" in script
                or "StackLayoutStackTitleBarbackBtn" in script
            ):
                return False
            return next(payloads)

    page = Page()
    client._scan(page, initial_scan=True)
    events.clear()
    client._scan(page, initial_scan=False)
    assert [event.message_key for event in events] == ["m-1", "m-2"]


def test_uncertain_event_is_persisted_until_due_reconciliation(tmp_path) -> None:
    now = [1_000.0]
    adapter = DouyinAdapter(_account(tmp_path), FakeBrowserClient(), clock=lambda: now[0])
    outcomes = iter(["delivery_uncertain", "auto_reply"])
    adapter._on_message = lambda _message: next(outcomes)

    adapter._accept_event(_event("uncertain-1"))
    assert "douyin:uncertain-1" in adapter._pending_events
    assert "uncertain-1" not in adapter._seen_by_conversation["conversation-1"]

    now[0] += adapter._DELIVERY_LEASE_SECONDS + 11
    adapter._queue_due_pending(force=False)
    replay = adapter._events.get_nowait()
    assert replay is not None
    adapter._accept_event(replay)
    assert "douyin:uncertain-1" not in adapter._pending_events
    assert "uncertain-1" in adapter._seen_by_conversation["conversation-1"]


def test_headless_callback_preserves_uncertain_event_for_reconciliation(tmp_path) -> None:
    class Hub:
        def handle(self, _message):
            return "delivery_uncertain"

    client = FakeBrowserClient()
    adapter = DouyinAdapter(_account(tmp_path), client)
    output = []
    adapter.start(
        lambda message: handle_message(
            Hub(), message, log=lambda value, **_kwargs: output.append(value),
        )
    )
    client.emit(_event("headless-uncertain"))
    deadline = time.monotonic() + 1.0
    while "douyin:headless-uncertain" not in adapter._pending_events:
        assert time.monotonic() < deadline
        time.sleep(0.01)

    assert "headless-uncertain" not in adapter._seen_by_conversation["conversation-1"]
    assert "delivery_uncertain" in output[0]
    adapter.stop()


def test_uncertain_browser_side_effect_consumes_rate_limit_quota() -> None:
    class Adapter:
        def send_message(self, _contact_id, _text, provenance="ai"):
            assert provenance == "ai"
            return SendResult(ok=False, uncertain=True, error="confirmation timed out")

    cfg = WidgetConfig(auto_send=True, send_delay_min_s=0, send_delay_max_s=0)
    limiter = RateLimiter(1, 10)
    sender = Sender(Adapter(), cfg, limiter, sleep_fn=lambda _seconds: None)
    assert sender.deliver("contact", "reply") is False
    assert limiter.can_send() is False


class FakeEditor:
    def click(self) -> None: pass
    def fill(self, _text: str) -> None: pass
    def press(self, _key: str) -> None: pass


class FakeEditors:
    def __init__(self) -> None:
        self.last = FakeEditor()

    def count(self) -> int:
        return 1


class FakeSendPage:
    def locator(self, _selector: str) -> FakeEditors:
        return FakeEditors()

    def wait_for_timeout(self, _milliseconds: int) -> None:
        pass


def test_send_confirmation_requires_a_new_stable_message_id(tmp_path) -> None:
    client = DouyinBrowserImClient(_account(tmp_path))
    page = FakeSendPage()
    old = [{"message_id": "old-1", "text": "同样回复", "is_self": True}]
    client._read = lambda _page, _target, **_kwargs: list(old)
    client._read_open_conversation = lambda _page, _target: list(old)
    command = _Command(
        "send", deadline=time.monotonic() + 0.02, client_message_id="client-1"
    )
    result = client._send(page, {"uid": "user-1"}, "同样回复", command)
    assert result.status == "unknown"

    client._read = lambda _page, _target, **_kwargs: list(old)
    client._read_open_conversation = lambda _page, _target: old + [
        {"message_id": "new-2", "text": "同样回复", "is_self": True},
    ]
    command = _Command(
        "send", deadline=time.monotonic() + 1.0, client_message_id="client-2"
    )
    result = client._send(page, {"uid": "user-1"}, "同样回复", command)
    assert result.status == "confirmed"


def test_send_confirmation_reads_the_verified_open_chat_without_reopening_it(tmp_path) -> None:
    client = DouyinBrowserImClient(_account(tmp_path))
    old = [{"message_id": "old-1", "text": "历史回复", "is_self": True}]
    read_calls = 0

    def read_before_send(_page, _target, **_kwargs):
        nonlocal read_calls
        read_calls += 1
        if read_calls > 1:
            raise AssertionError("发送后不应重新遍历并打开会话")
        return list(old)

    client._read = read_before_send

    class Page:
        sent = False

        class Editor:
            def __init__(self, owner):
                self.owner = owner

            def click(self): pass
            def fill(self, _text): pass
            def press(self, _key): self.owner.sent = True

        class Editors:
            def __init__(self, owner):
                self.last = Page.Editor(owner)

            def count(self): return 1

        def locator(self, _selector):
            return self.Editors(self)

        def wait_for_timeout(self, _milliseconds): pass

        def evaluate(self, _script):
            messages = [{"stable": "old-1", "text": "历史回复", "own": True}]
            if self.sent:
                messages.append({"stable": "new-2", "text": "闲聊回复", "own": True})
            return {"messages": messages, "title": "Visitor"}

    result = client._send(
        Page(),
        {"uid": "user-1", "name": "Visitor"},
        "闲聊回复",
        _Command("send", deadline=time.monotonic() + 1, client_message_id="client-open"),
    )

    assert result.status == "confirmed"
    assert read_calls == 1


def test_dom_fingerprint_target_requires_explicit_authorization(tmp_path) -> None:
    client = DouyinBrowserImClient(_account(tmp_path))
    page = FakeSendPage()
    target = {
        "uid": "dom:stable-fingerprint",
        "name": "Visitor",
        "index": 0,
        "locator_kind": "conversation_item",
        "list_scope": "stranger",
    }
    blocked = client._send(
        page,
        target,
        "人工回复",
        _Command("send", deadline=time.monotonic() + 1, client_message_id="blocked"),
    )
    assert blocked.status == "failed"
    assert "未获得精确 DOM 会话授权" in blocked.error

    old = [{"message_id": "old-1", "text": "历史回复", "is_self": True}]
    client._read = lambda _page, _target, **_kwargs: list(old)
    client._read_open_conversation = lambda _page, _target: old + [
        {"message_id": "new-2", "text": "人工回复", "is_self": True},
    ]
    allowed = client._send(
        page,
        target,
        "人工回复",
        _Command("send", deadline=time.monotonic() + 1, client_message_id="allowed"),
        allow_dom_target=True,
    )
    assert allowed.status == "confirmed"


def test_delivery_reconciliation_only_timestamps_ids_newer_than_send_window(tmp_path) -> None:
    account = _account(tmp_path)
    client = DouyinBrowserImClient(account, clock=lambda: 1_700_000_123)
    client._register_delivery_check(
        "client-1", uid="user-1", text="同样回复", before_ids={"old-1"}
    )
    client._open_target = lambda _page, _target, **_kwargs: None

    class Page:
        def evaluate(self, _script):
            return {"messages": [
                {"stable": "old-1", "text": "同样回复", "own": True},
                {"stable": "new-2", "text": "同样回复", "own": True},
            ]}

    rows = client._read(Page(), {"uid": "user-1", "name": "Alice"})
    assert rows[0]["ts"] == 0
    assert rows[1]["ts"] == 1_700_000_123

    recovered = DouyinBrowserImClient(account, clock=lambda: 1_800_000_000)
    recovered._open_target = lambda _page, _target, **_kwargs: None
    rows = recovered._read(Page(), {"uid": "user-1", "name": "Alice"})
    assert rows[1]["ts"] == 1_700_000_123


def test_adapter_reconciliation_uses_only_verified_cached_dom_bubbles(tmp_path) -> None:
    client = FakeBrowserClient()
    adapter = DouyinAdapter(_account(tmp_path), client)
    contact = "im:dom:verified"
    adapter._targets[contact] = {
        "uid": "dom:verified",
        "name": "Visitor",
        "locator_kind": "conversation_item",
    }
    adapter._history[contact] = [{
        "local_id": "douyin:rendered-new-id",
        "text": "已经发送的闲聊回复",
        "is_self": True,
        "ts": 1_700_000_010,
        "provenance": "reconciled",
    }]
    client.read_conversation = lambda _target: (_ for _ in ()).throw(
        RuntimeError("browser command queue is busy")
    )

    assert adapter.reconcile_delivery(
        contact, "已经发送的闲聊回复", 1_700_000_000
    ) == "delivered"

    adapter._history[contact] = [{
        "local_id": "local:unverified",
        "text": "只有本地草稿",
        "is_self": True,
        "ts": 1_700_000_010,
        "provenance": "ai",
    }]
    assert adapter.reconcile_delivery(
        contact, "只有本地草稿", 1_700_000_000
    ) == "unknown"


def test_timed_out_queued_send_is_cancelled_before_side_effect(tmp_path) -> None:
    client = DouyinBrowserImClient(_account(tmp_path))
    client._thread = threading.current_thread()
    with pytest.raises(Exception, match="已取消"):
        client._request("send", {"uid": "user-1"}, "hello", timeout_s=0.01)
    command = client._commands.get_nowait()
    assert command.cancelled is True
    assert command.side_effect_started is False


def test_failed_manual_scan_does_not_consume_initial_baseline(tmp_path) -> None:
    client = DouyinBrowserImClient(_account(tmp_path))
    client._refresh_identity = lambda _context: True
    seen_initial = []

    def scan(_page, *, initial_scan: bool) -> int:
        seen_initial.append(initial_scan)
        if len(seen_initial) == 1:
            raise RuntimeError("inbox not ready")
        return 0

    client._scan = scan
    first = _Command("scan", deadline=time.monotonic() + 1)
    client._handle_command(object(), object(), first, True)
    assert first.error is not None
    initial_scan = not (first.operation == "scan" and first.error is None)

    second = _Command("scan", deadline=time.monotonic() + 1)
    client._handle_command(object(), object(), second, initial_scan)
    assert second.error is None
    assert seen_initial == [True, True]
