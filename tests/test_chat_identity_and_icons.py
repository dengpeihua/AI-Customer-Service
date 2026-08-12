from __future__ import annotations

import datetime as dt
import hashlib
import importlib
import os
import threading
import time
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QGuiApplication, QIcon, QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from widget.adapters.wechat_hook import WeChatHookAdapter
from widget.config import WidgetConfig
from widget.state import RuntimeState
from widget.ui.app_icon import (
    apply_windows_window_icon,
    brand_mark_pixmap,
    load_app_icon,
    project_icon_path,
    status_icon,
    windows_icon_path,
)
from widget.ui.pages.history_page import HistoryPage, _bubble_row
from widget.ui.tray import Tray


def _adapter() -> WeChatHookAdapter:
    return WeChatHookAdapter(
        WidgetConfig(), patch_login=False,
        self_wxid_override="profile_id_that_differs_from_message_db",
    )


class ChatSenderIdentityTests(unittest.TestCase):
    def test_group_history_keeps_member_id_and_resolves_each_name(self) -> None:
        adapter = _adapter()
        contact = "123@chatroom"
        suffix = hashlib.md5(contact.encode("utf-8")).hexdigest()
        adapter._id2name = {1: "wxid_alice", 2: "local_self_alias"}
        adapter._list_suffixes = lambda: [suffix]
        adapter._query = lambda _sql: [
            {"local_id": 1, "local_type": 1, "real_sender_id": 1,
             "create_time": 1, "mc": "wxid_alice:\nhello", "ct": 0, "mc_hex": ""},
            {"local_id": 2, "local_type": 1, "real_sender_id": 2,
             "create_time": 2, "mc": "reply", "ct": 0, "mc_hex": ""},
        ]
        adapter._is_self_sender = lambda sender, _session: sender == "local_self_alias"
        adapter.display_names = lambda ids: {value: {"wxid_alice": "Alice"}.get(value, value)
                                             for value in ids}
        adapter._media.enrich = lambda message: message

        messages = adapter._read_conversation_legacy(contact, 80)

        self.assertEqual("wxid_alice", messages[0]["sender_id"])
        self.assertEqual("Alice", messages[0]["sender_name"])
        self.assertEqual("hello", messages[0]["text"])
        self.assertTrue(messages[0]["is_group"])
        self.assertEqual("local_self_alias", messages[1]["sender_id"])
        self.assertEqual("我", messages[1]["sender_name"])

    def test_known_fts_contact_skips_redundant_legacy_table_scan(self) -> None:
        adapter = _adapter()
        adapter.guard_ok = Mock(return_value=True)
        adapter._fts_name2id = {"wxid_customer": 7}
        # Name2Id 只说明联系人存在，不能证明对应的旧式 Msg_<md5> 表存在。
        adapter._md52name = {
            hashlib.md5(b"wxid_customer").hexdigest(): "wxid_customer"
        }
        adapter._legacy_suffixes = set()
        expected = [{"text": "fts history"}]
        adapter._read_conversation_legacy = Mock(side_effect=AssertionError(
            "known FTS contact should not scan legacy tables first"
        ))
        adapter._read_conversation_fts = Mock(return_value=expected)

        result = adapter.read_conversation("wxid_customer")

        self.assertEqual(expected, result)
        adapter._read_conversation_legacy.assert_not_called()
        adapter._read_conversation_fts.assert_called_once_with("wxid_customer", 80)

    def test_legacy_table_discovery_caches_real_message_suffixes(self) -> None:
        adapter = _adapter()
        first, second = "a" * 32, "b" * 32
        adapter._query = Mock(return_value=[
            {"name": f"Msg_{first}"},
            {"name": "unrelated_table"},
            {"name": f"Msg_{second}"},
        ])

        self.assertEqual([first, second], adapter._list_suffixes_strict())
        self.assertEqual({first, second}, adapter._legacy_suffixes)

    def test_fts_history_reuses_the_table_list_found_by_background_polling(self) -> None:
        adapter = _adapter()
        adapter._fts_name2id = {"wxid_customer": 7}
        adapter._list_fts_tables = Mock(return_value=["FTS5IndexMessage_0"])
        adapter._query_db = Mock(return_value=[])

        adapter._read_conversation_fts("wxid_customer", 80)

        adapter._list_fts_tables.assert_called_once_with(refresh=False)


class _FakeAvatars:
    def get_pixmap(self, _wxid: str, _name: str) -> QPixmap:
        pixmap = QPixmap(36, 36)
        pixmap.fill()
        return pixmap


class ChatBubbleIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_incoming_group_bubble_shows_member_name_and_avatar(self) -> None:
        row = _bubble_row(
            {"kind": "text", "text": "hello", "sender_id": "wxid_alice",
             "sender_name": "Alice", "is_self": False},
            "customer", False, _FakeAvatars(),
        )
        labels = [label.text() for label in row.findChildren(QLabel)]
        self.assertIn("Alice", labels)
        self.assertNotIn("客户", labels)
        self.assertEqual("wxid_alice", row._avatar_wxid)
        self.assertFalse(row._avatar_label.pixmap().isNull())

    def test_self_bubble_is_labeled_as_me_with_source(self) -> None:
        row = _bubble_row(
            {"kind": "text", "text": "reply", "sender_id": "self_alias",
             "sender_name": "我", "is_self": True},
            "agent", True, _FakeAvatars(),
        )
        self.assertIn("我 · 人工", [label.text() for label in row.findChildren(QLabel)])


class _HistoryAdapter:
    channel = "wechat_personal"

    def list_sessions(self) -> list[dict]:
        return [{"wxid": "wxid_customer", "summary": "最近一条", "ts": 2}]

    def display_names(self, ids: list[str]) -> dict[str, str]:
        return {value: "客户甲" for value in ids}

    def read_conversation(self, _contact: str, limit: int = 80) -> list[dict]:
        return [{
            "kind": "text",
            "text": "已经存在的聊天记录",
            "sender_id": "wxid_customer",
            "sender_name": "客户甲",
            "is_self": False,
            "is_group": False,
            "ts": 1,
        }]

    def provenance_for(self, _text: str):
        return None


class _UnavailableHistoryAdapter(_HistoryAdapter):
    def list_sessions(self) -> list[dict]:
        raise RuntimeError("所选微信 PID 路由尚未就绪")


class _EmptyHistoryAdapter(_HistoryAdapter):
    def list_sessions(self) -> list[dict]:
        return []


class _HistoryBridge:
    def get_ai_mute(self, _channel: str, _contact: str) -> bool:
        return False


class HistoryStartupDisplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _wait_until(self, predicate, timeout: float = 1.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            QApplication.processEvents()
            if predicate():
                return True
            time.sleep(0.005)
        QApplication.processEvents()
        return bool(predicate())

    def test_first_recent_conversation_is_visible_without_manual_reply(self) -> None:
        page = HistoryPage(
            _HistoryBridge(), adapter=_HistoryAdapter(),
            state=RuntimeState(WidgetConfig()),
        )
        try:
            self.assertTrue(self._wait_until(lambda: any(
                label.text() == "已经存在的聊天记录"
                for label in page.findChildren(QLabel)
            )))
            self.assertEqual("wxid_customer", page._current_wxid)
            self.assertIn(
                "已经存在的聊天记录",
                [label.text() for label in page.findChildren(QLabel)],
            )
        finally:
            page.close()
            page.deleteLater()

    def test_deferred_initial_load_does_not_query_wechat_before_window_is_ready(self) -> None:
        adapter = _HistoryAdapter()
        adapter.list_sessions = Mock(wraps=adapter.list_sessions)
        page = HistoryPage(
            _HistoryBridge(), adapter=adapter,
            state=RuntimeState(WidgetConfig()), defer_initial_load=True,
        )
        try:
            adapter.list_sessions.assert_not_called()
            self.assertIn("正在加载会话", page._list.item(0).text())
        finally:
            page.close()
            page.deleteLater()

    def test_refresh_button_path_returns_before_slow_session_query_finishes(self) -> None:
        release = threading.Event()

        class SlowSessionsAdapter(_HistoryAdapter):
            def list_sessions(self) -> list[dict]:
                release.wait(1.0)
                return super().list_sessions()

        page = HistoryPage(
            _HistoryBridge(), adapter=SlowSessionsAdapter(),
            state=RuntimeState(WidgetConfig()), defer_initial_load=True,
        )
        try:
            started = time.monotonic()
            page.request_refresh()
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 0.1)
            self.assertIn("后台刷新", page._send_status.text())
        finally:
            release.set()
            page.close()
            page.deleteLater()

    def test_live_inbound_event_renders_without_waiting_for_database_read(self) -> None:
        page = HistoryPage(
            _HistoryBridge(), adapter=_HistoryAdapter(),
            state=RuntimeState(WidgetConfig()), defer_initial_load=True,
        )
        try:
            page.apply_live_event({
                "event_id": "in:wechat_personal:m-live",
                "direction": "inbound",
                "channel": "wechat_personal",
                "contact_id": "wxid_live",
                "sender_id": "wxid_live",
                "text": "立即出现在会话框",
                "timestamp": 10,
            })

            self.assertEqual("wxid_live", page._current_wxid)
            self.assertIn("wxid_live", page._list.item(0).text())
            self.assertIn(
                "立即出现在会话框",
                [label.text() for label in page.findChildren(QLabel)],
            )
        finally:
            page.close()
            page.deleteLater()

    def test_connection_failure_is_not_reported_as_no_conversations(self) -> None:
        page = HistoryPage(
            _HistoryBridge(), adapter=_UnavailableHistoryAdapter(),
            state=RuntimeState(WidgetConfig()),
        )
        try:
            status = page._list.item(0).text()
            self.assertIn("会话读取未就绪", status)
            self.assertIn("PID 路由尚未就绪", status)
            self.assertNotIn("暂无会话", status)
        finally:
            page.close()
            page.deleteLater()

    def test_hook_timeout_falls_back_to_saved_backend_conversation(self) -> None:
        class SavedConversationBridge(_HistoryBridge):
            def list_conversations(self, limit: int = 50, offset: int = 0) -> list[dict]:
                return [{
                    "id": 17,
                    "contact_id": "wxid_saved",
                    "channel": "wechat_personal",
                    "status": "active",
                    "last_text": "客服系统保存的最近消息",
                    "last_at": "2026-08-11T14:00:00+00:00",
                    "message_count": 1,
                }]

            def get_conversation(self, conv_id: int) -> dict:
                self.last_requested_id = conv_id
                return {
                    "id": conv_id,
                    "contact_id": "wxid_saved",
                    "channel": "wechat_personal",
                    "messages": [{
                        "id": 9,
                        "direction": "in",
                        "sender": "customer",
                        "text": "客服系统保存的最近消息",
                        "created_at": "2026-08-11T14:00:00+00:00",
                    }],
                }

        bridge = SavedConversationBridge()
        page = HistoryPage(
            bridge, adapter=_UnavailableHistoryAdapter(),
            state=RuntimeState(WidgetConfig()), defer_initial_load=True,
        )
        try:
            page.live_tick()
            self.assertTrue(self._wait_until(lambda: any(
                label.text() == "客服系统保存的最近消息"
                for label in page.findChildren(QLabel)
            )))

            self.assertEqual(["wxid_saved"], [row["wxid"] for row in page._sessions])
            self.assertEqual(17, bridge.last_requested_id)
            self.assertIn("已显示客服系统记录", page._send_status.text())
            self.assertNotIn("会话读取未就绪", page._list.item(0).text())
        finally:
            page.close()
            page.deleteLater()

    def test_saved_conversation_fallback_ignores_malformed_backend_ids(self) -> None:
        class MalformedBridge(_HistoryBridge):
            def list_conversations(self, limit: int = 50, offset: int = 0) -> list[dict]:
                return [{
                    "id": "not-an-integer",
                    "contact_id": "wxid_saved",
                    "channel": "wechat_personal",
                    "last_text": "saved",
                    "last_at": "not-a-timestamp",
                }]

        page = HistoryPage(
            MalformedBridge(), adapter=_UnavailableHistoryAdapter(),
            state=RuntimeState(WidgetConfig()), defer_initial_load=True,
        )
        try:
            sessions = page._load_backend_sessions("wechat_personal")

            self.assertEqual(0, sessions[0]["backend_conversation_id"])
            self.assertEqual(0, sessions[0]["ts"])
            self.assertIsNone(page._load_backend_messages("wechat_personal", "wxid_saved"))
        finally:
            page.close()
            page.deleteLater()

    def test_successful_empty_query_still_reports_no_conversations(self) -> None:
        page = HistoryPage(
            _HistoryBridge(), adapter=_EmptyHistoryAdapter(),
            state=RuntimeState(WidgetConfig()),
        )
        try:
            self.assertEqual("（暂无会话）", page._list.item(0).text())
        finally:
            page.close()
            page.deleteLater()

    def test_live_tick_does_not_replace_visible_sessions_with_transient_empty_result(self) -> None:
        adapter = _HistoryAdapter()
        page = HistoryPage(
            _HistoryBridge(), adapter=adapter,
            state=RuntimeState(WidgetConfig()),
        )
        try:
            page._apply_tick({
                "adapter": adapter,
                "cur": page._current_wxid,
                "active": None,
                "msgs": None,
                "sessions": [],
                "names": {},
            })

            self.assertEqual(["wxid_customer"], [s["wxid"] for s in page._sessions])
            self.assertNotEqual("（暂无会话）", page._list.item(0).text())
        finally:
            page.close()
            page.deleteLater()

    def test_initial_live_tick_fetches_session_list_before_slower_follow_probes(self) -> None:
        calls: list[str] = []
        finished = threading.Event()

        class PrioritizedAdapter(_HistoryAdapter):
            def list_sessions(self) -> list[dict]:
                calls.append("sessions")
                return super().list_sessions()

            def active_session(self) -> str:
                calls.append("active")
                return ""

            def display_names(self, ids: list[str]) -> dict[str, str]:
                calls.append("names")
                result = super().display_names(ids)
                finished.set()
                return result

        page = HistoryPage(
            _HistoryBridge(), adapter=PrioritizedAdapter(),
            state=RuntimeState(WidgetConfig()), defer_initial_load=True,
        )
        try:
            page.live_tick()
            self.assertTrue(finished.wait(1.0))
            self.assertEqual("sessions", calls[0])
        finally:
            page.close()
            page.deleteLater()

    def test_initial_tick_publishes_sessions_before_slow_chat_read_finishes(self) -> None:
        read_started = threading.Event()
        release_read = threading.Event()

        class SlowChatAdapter(_HistoryAdapter):
            def read_conversation(self, contact: str, limit: int = 80) -> list[dict]:
                read_started.set()
                release_read.wait(1.0)
                return super().read_conversation(contact, limit)

        page = HistoryPage(
            _HistoryBridge(), adapter=SlowChatAdapter(),
            state=RuntimeState(WidgetConfig()), defer_initial_load=True,
        )
        try:
            page.live_tick()
            self.assertTrue(read_started.wait(1.0))
            QApplication.processEvents()

            self.assertEqual(
                ["wxid_customer"],
                [session["wxid"] for session in page._sessions],
            )
            self.assertNotIn("正在加载会话", page._list.item(0).text())
        finally:
            release_read.set()
            page.close()
            page.deleteLater()

    def test_initial_tick_publishes_chat_before_slow_active_probe_finishes(self) -> None:
        probe_started = threading.Event()
        release_probe = threading.Event()

        class SlowActiveProbeAdapter(_HistoryAdapter):
            def active_session(self) -> str:
                probe_started.set()
                release_probe.wait(1.0)
                return ""

        page = HistoryPage(
            _HistoryBridge(), adapter=SlowActiveProbeAdapter(),
            state=RuntimeState(WidgetConfig()), defer_initial_load=True,
        )
        try:
            page.live_tick()
            self.assertTrue(probe_started.wait(1.0))
            QApplication.processEvents()

            self.assertEqual("wxid_customer", page._current_wxid)
            self.assertIn(
                "已经存在的聊天记录",
                [label.text() for label in page.findChildren(QLabel)],
            )
        finally:
            release_probe.set()
            page.close()
            page.deleteLater()

    def test_initial_tick_renders_prefetched_chat_without_gui_database_read(self) -> None:
        adapter = _HistoryAdapter()
        adapter.read_conversation = Mock(side_effect=AssertionError(
            "GUI 线程不应再次读取微信聊天数据库"
        ))
        page = HistoryPage(
            _HistoryBridge(), adapter=adapter,
            state=RuntimeState(WidgetConfig()), defer_initial_load=True,
        )
        prefetched = [{
            "kind": "text",
            "text": "后台已经取到的聊天",
            "sender_id": "wxid_customer",
            "sender_name": "客户甲",
            "is_self": False,
            "is_group": False,
            "ts": 1,
        }]
        try:
            page._apply_tick({
                "adapter": adapter,
                "cur": "",
                "active": "wxid_customer",
                "target": "wxid_customer",
                "msgs": prefetched,
                "sessions": adapter.list_sessions(),
                "names": {"wxid_customer": "客户甲"},
            })

            adapter.read_conversation.assert_not_called()
            self.assertEqual("wxid_customer", page._current_wxid)
            self.assertIn(
                "后台已经取到的聊天",
                [label.text() for label in page.findChildren(QLabel)],
            )
        finally:
            page.close()
            page.deleteLater()

    def test_prefetched_chat_render_is_not_blocked_by_slow_ai_state_request(self) -> None:
        release = threading.Event()

        class SlowBridge(_HistoryBridge):
            def get_ai_mute(self, _channel: str, _contact: str) -> bool:
                release.wait(0.4)
                return False

        adapter = _HistoryAdapter()
        page = HistoryPage(
            SlowBridge(), adapter=adapter,
            state=RuntimeState(WidgetConfig()), defer_initial_load=True,
        )
        prefetched = adapter.read_conversation("wxid_customer")
        timer = threading.Timer(0.3, release.set)
        try:
            timer.start()
            started = time.monotonic()
            page._apply_tick({
                "adapter": adapter,
                "cur": "",
                "active": None,
                "target": "wxid_customer",
                "msgs": prefetched,
                "sessions": adapter.list_sessions(),
                "names": {"wxid_customer": "客户甲"},
            })
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 0.2)
            self.assertIn(
                "已经存在的聊天记录",
                [label.text() for label in page.findChildren(QLabel)],
            )
        finally:
            release.set()
            timer.join(1.0)
            page.close()
            page.deleteLater()

    def test_clicking_slow_conversation_returns_immediately_and_shows_loading(self) -> None:
        read_started = threading.Event()
        release_read = threading.Event()
        read_finished = threading.Event()

        class SlowClickAdapter(_HistoryAdapter):
            def list_sessions(self) -> list[dict]:
                return [
                    {"wxid": "wxid_first", "summary": "第一位", "ts": 2},
                    {"wxid": "wxid_second", "summary": "第二位", "ts": 1},
                ]

            def display_names(self, ids: list[str]) -> dict[str, str]:
                return {value: value for value in ids}

            def read_conversation(self, contact: str, limit: int = 80) -> list[dict]:
                read_started.set()
                release_read.wait(0.4)
                read_finished.set()
                return [{
                    "kind": "text",
                    "text": f"{contact} 的聊天内容",
                    "sender_id": contact,
                    "sender_name": contact,
                    "is_self": False,
                    "is_group": False,
                    "ts": 1,
                }]

        adapter = SlowClickAdapter()
        page = HistoryPage(
            _HistoryBridge(), adapter=adapter,
            state=RuntimeState(WidgetConfig()), defer_initial_load=True,
        )
        page.refresh(
            sessions=adapter.list_sessions(),
            names=adapter.display_names(["wxid_first", "wxid_second"]),
            select_first=False,
        )
        try:
            started = time.monotonic()
            page._list.setCurrentRow(1)
            elapsed = time.monotonic() - started
            self.assertTrue(read_started.wait(0.2))
            QApplication.processEvents()

            self.assertLess(elapsed, 0.15)
            self.assertTrue(any(
                "正在加载聊天记录" in label.text()
                for label in page.findChildren(QLabel)
            ))

            release_read.set()
            self.assertTrue(read_finished.wait(1.0))
            for _ in range(20):
                QApplication.processEvents()
                if any(
                    "wxid_second 的聊天内容" in label.text()
                    for label in page.findChildren(QLabel)
                ):
                    break
                time.sleep(0.01)
            self.assertIn(
                "wxid_second 的聊天内容",
                [label.text() for label in page.findChildren(QLabel)],
            )
        finally:
            release_read.set()
            page.close()
            page.deleteLater()

    def test_saved_backend_message_is_shown_before_slow_local_history(self) -> None:
        local_started = threading.Event()
        release_local = threading.Event()

        class SlowLocalAdapter(_HistoryAdapter):
            def read_conversation(self, contact: str, limit: int = 80) -> list[dict]:
                local_started.set()
                release_local.wait(0.8)
                return super().read_conversation(contact, limit)

        class SavedBridge(_HistoryBridge):
            def list_conversations(self, limit: int = 50, offset: int = 0) -> list[dict]:
                return [{
                    "id": 21, "contact_id": "wxid_customer",
                    "channel": "wechat_personal", "last_text": "后端快照先显示",
                    "last_at": "2026-08-12T00:00:00+00:00",
                }]

            def get_conversation(self, conv_id: int) -> dict:
                return {
                    "id": conv_id,
                    "channel": "wechat_personal",
                    "contact_id": "wxid_customer",
                    "messages": [{
                        "id": 1, "direction": "in", "sender": "customer",
                        "text": "后端快照先显示", "created_at": "2026-08-12T00:00:00+00:00",
                    }],
                }

        adapter = SlowLocalAdapter()
        page = HistoryPage(
            SavedBridge(), adapter=adapter,
            state=RuntimeState(WidgetConfig()), defer_initial_load=True,
        )
        try:
            page.refresh(
                sessions=adapter.list_sessions(),
                names={"wxid_customer": "客户甲"},
                select_first=False,
            )
            page._list.setCurrentRow(0)
            self.assertTrue(local_started.wait(0.2))
            self.assertTrue(self._wait_until(lambda: any(
                label.text() == "后端快照先显示"
                for label in page.findChildren(QLabel)
            )))
            self.assertFalse(release_local.is_set())
        finally:
            release_local.set()
            page.close()
            page.deleteLater()

    def test_late_local_snapshot_merges_with_backend_instead_of_replacing_it(self) -> None:
        class SavedBridge(_HistoryBridge):
            def list_conversations(self, limit: int = 50, offset: int = 0) -> list[dict]:
                return [{
                    "id": 22, "contact_id": "wxid_customer",
                    "channel": "wechat_personal", "last_text": "AI 已确认送达",
                    "last_at": "2026-08-12T00:00:02+00:00",
                }]

            def get_conversation(self, conv_id: int) -> dict:
                return {
                    "id": conv_id,
                    "channel": "wechat_personal",
                    "contact_id": "wxid_customer",
                    "messages": [
                        {"id": 1, "direction": "in", "sender": "customer",
                         "text": "营业时间？", "created_at": "2026-08-12T00:00:00+00:00"},
                        {"id": 2, "direction": "out", "sender": "ai", "provenance": "ai",
                         "delivery_status": "delivered", "text": "AI 已确认送达",
                         "created_at": "2026-08-12T00:00:02+00:00"},
                    ],
                }

        adapter = _HistoryAdapter()
        adapter.read_conversation = Mock(return_value=[{
            "kind": "text", "local_id": "wechat:1", "text": "营业时间？",
            "sender_id": "wxid_customer", "sender_name": "客户甲", "is_self": False,
            "is_group": False, "ts": int(dt.datetime(2026, 8, 12, tzinfo=dt.UTC).timestamp()),
        }])
        page = HistoryPage(
            SavedBridge(), adapter=adapter,
            state=RuntimeState(WidgetConfig()), defer_initial_load=True,
        )
        try:
            page.refresh(
                sessions=adapter.list_sessions(), names={"wxid_customer": "客户甲"},
                select_first=False,
            )
            page._list.setCurrentRow(0)
            self.assertTrue(self._wait_until(lambda: {
                "营业时间？", "AI 已确认送达"
            }.issubset({label.text() for label in page.findChildren(QLabel)})))
            self.assertEqual(
                1,
                [message["text"] for message in page._conversation_cache[
                    ("wechat_personal", "wxid_customer")
                ]].count("营业时间？"),
            )
        finally:
            page.close()
            page.deleteLater()

    def test_backend_loader_keeps_human_reply_without_delivery_receipt(self) -> None:
        class SavedBridge(_HistoryBridge):
            def list_conversations(self, limit: int = 50, offset: int = 0) -> list[dict]:
                return [{
                    "id": 23, "contact_id": "wxid_customer",
                    "channel": "wechat_personal", "last_text": "人工已回复",
                    "last_at": "2026-08-12T00:00:02+00:00",
                }]

            def get_conversation(self, conv_id: int) -> dict:
                return {
                    "id": conv_id, "channel": "wechat_personal",
                    "contact_id": "wxid_customer", "messages": [{
                        "id": 3, "direction": "out", "sender": "agent",
                        "provenance": "human", "delivery_status": None,
                        "text": "人工已回复", "created_at": "2026-08-12T00:00:02+00:00",
                    }],
                }

        page = HistoryPage(
            SavedBridge(), adapter=_HistoryAdapter(),
            state=RuntimeState(WidgetConfig()), defer_initial_load=True,
        )
        try:
            messages = page._load_backend_messages("wechat_personal", "wxid_customer")

            self.assertEqual(["人工已回复"], [message["text"] for message in messages])
        finally:
            page.close()
            page.deleteLater()

    def test_rapid_click_discards_late_result_from_previous_conversation(self) -> None:
        first_started = threading.Event()
        release_first = threading.Event()

        class SwitchingAdapter(_HistoryAdapter):
            def list_sessions(self) -> list[dict]:
                return [
                    {"wxid": "wxid_first", "summary": "第一位", "ts": 2},
                    {"wxid": "wxid_second", "summary": "第二位", "ts": 1},
                ]

            def read_conversation(self, contact: str, limit: int = 80) -> list[dict]:
                if contact == "wxid_first":
                    first_started.set()
                    release_first.wait(0.5)
                return [{
                    "kind": "text",
                    "text": f"{contact} 的最终内容",
                    "sender_id": contact,
                    "sender_name": contact,
                    "is_self": False,
                    "is_group": False,
                    "ts": 1,
                }]

        adapter = SwitchingAdapter()
        page = HistoryPage(
            _HistoryBridge(), adapter=adapter,
            state=RuntimeState(WidgetConfig()), defer_initial_load=True,
        )
        page.refresh(
            sessions=adapter.list_sessions(),
            names={"wxid_first": "第一位", "wxid_second": "第二位"},
            select_first=False,
        )
        try:
            page._list.setCurrentRow(0)
            self.assertTrue(first_started.wait(0.2))
            page._list.setCurrentRow(1)
            self.assertTrue(self._wait_until(lambda: any(
                label.text() == "wxid_second 的最终内容"
                for label in page.findChildren(QLabel)
            )))

            release_first.set()
            self.assertTrue(self._wait_until(lambda: page._current_wxid == "wxid_second"))
            labels = [label.text() for label in page.findChildren(QLabel)]
            self.assertIn("wxid_second 的最终内容", labels)
            self.assertNotIn("wxid_first 的最终内容", labels)
        finally:
            release_first.set()
            page.close()
            page.deleteLater()

    def test_cached_chat_stays_visible_during_slow_background_refresh(self) -> None:
        second_started = threading.Event()
        release_second = threading.Event()

        class CachedAdapter(_HistoryAdapter):
            def __init__(self) -> None:
                self.reads = 0

            def read_conversation(self, contact: str, limit: int = 80) -> list[dict]:
                self.reads += 1
                if self.reads > 1:
                    second_started.set()
                    release_second.wait(0.5)
                return [{
                    "kind": "text",
                    "text": "缓存中的聊天内容",
                    "sender_id": contact,
                    "sender_name": contact,
                    "is_self": False,
                    "is_group": False,
                    "ts": 1,
                }]

        adapter = CachedAdapter()
        page = HistoryPage(
            _HistoryBridge(), adapter=adapter,
            state=RuntimeState(WidgetConfig()),
        )
        try:
            self.assertTrue(self._wait_until(lambda: any(
                label.text() == "缓存中的聊天内容"
                for label in page.findChildren(QLabel)
            )))

            started = time.monotonic()
            page.select_contact("wxid_customer")
            elapsed = time.monotonic() - started

            self.assertTrue(second_started.wait(0.2))
            self.assertLess(elapsed, 0.15)
            self.assertIn(
                "缓存中的聊天内容",
                [label.text() for label in page.findChildren(QLabel)],
            )
        finally:
            release_second.set()
            page.close()
            page.deleteLater()

    def test_returning_to_history_keeps_visible_chat_while_background_refresh_runs(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        worker_finished = threading.Event()

        class SlowRefreshAdapter(_HistoryAdapter):
            def __init__(self) -> None:
                self.session_reads = 0

            def list_sessions(self) -> list[dict]:
                self.session_reads += 1
                if self.session_reads > 1:
                    entered.set()
                    release.wait(1.0)
                    return []
                return [
                    {"wxid": "wxid_first", "summary": "第一位", "ts": 2},
                    {"wxid": "wxid_second", "summary": "第二位", "ts": 1},
                ]

            def active_session(self) -> str:
                return ""

            def read_conversation(self, contact: str, limit: int = 80) -> list[dict]:
                if self.session_reads > 1:
                    worker_finished.set()
                return [{
                    "kind": "text",
                    "text": f"{contact} 的聊天",
                    "sender_id": contact,
                    "sender_name": contact,
                    "is_self": False,
                    "is_group": False,
                    "ts": 1,
                }]

        adapter = SlowRefreshAdapter()
        page = HistoryPage(
            _HistoryBridge(), adapter=adapter,
            state=RuntimeState(WidgetConfig()),
        )
        try:
            page.select_contact("wxid_second")
            self.assertTrue(self._wait_until(lambda: any(
                label.text() == "wxid_second 的聊天"
                for label in page.findChildren(QLabel)
            )))
            page.activate()
            self.assertTrue(entered.wait(1.0))
            self.assertEqual("wxid_second", page._current_wxid)
            self.assertIn(
                "wxid_second 的聊天",
                [label.text() for label in page.findChildren(QLabel)],
            )
            release.set()
            self.assertTrue(worker_finished.wait(1.0))
            QApplication.processEvents()
            self.assertEqual("wxid_second", page._current_wxid)
            self.assertIn(
                "wxid_second 的聊天",
                [label.text() for label in page.findChildren(QLabel)],
            )
        finally:
            release.set()
            page.close()
            page.deleteLater()

    def test_refresh_failure_preserves_visible_session_and_chat_snapshot(self) -> None:
        adapter = _HistoryAdapter()
        page = HistoryPage(
            _HistoryBridge(), adapter=adapter,
            state=RuntimeState(WidgetConfig()),
        )
        try:
            self.assertTrue(self._wait_until(lambda: bool(page._last_msgs)))
            adapter.list_sessions = Mock(side_effect=RuntimeError("temporary read failure"))
            page.refresh()

            self.assertEqual(
                ["wxid_customer"],
                [session["wxid"] for session in page._sessions],
            )
            self.assertIn(
                "已经存在的聊天记录",
                [label.text() for label in page.findChildren(QLabel)],
            )
        finally:
            page.close()
            page.deleteLater()

    def test_transient_empty_reread_preserves_current_chat_snapshot(self) -> None:
        adapter = _HistoryAdapter()
        page = HistoryPage(
            _HistoryBridge(), adapter=adapter,
            state=RuntimeState(WidgetConfig()),
        )
        try:
            self.assertTrue(self._wait_until(lambda: bool(page._last_msgs)))
            adapter.read_conversation = Mock(return_value=[])
            page.select_contact("wxid_customer")
            self.assertTrue(self._wait_until(lambda: (
                page._send_status.text() == "聊天刷新暂未完成，继续显示上次内容"
            )))

            self.assertNotEqual([], page._last_msgs)
            self.assertIn(
                "已经存在的聊天记录",
                [label.text() for label in page.findChildren(QLabel)],
            )
        finally:
            page.close()
            page.deleteLater()

    def test_manual_refresh_can_still_show_a_confirmed_empty_session_list(self) -> None:
        adapter = _HistoryAdapter()
        page = HistoryPage(
            _HistoryBridge(), adapter=adapter,
            state=RuntimeState(WidgetConfig()),
        )
        try:
            adapter.list_sessions = lambda: []
            page.refresh()

            self.assertEqual([], page._sessions)
            self.assertEqual("（暂无会话）", page._list.item(0).text())
        finally:
            page.close()
            page.deleteLater()

    def test_manual_refresh_rechecks_transient_empty_before_clearing_sessions(self) -> None:
        adapter = _HistoryAdapter()
        page = HistoryPage(
            _HistoryBridge(), adapter=adapter,
            state=RuntimeState(WidgetConfig()),
        )
        try:
            visible_sessions = list(page._sessions)
            adapter.list_sessions = Mock(side_effect=[[], visible_sessions])

            page.refresh()

            self.assertEqual(2, adapter.list_sessions.call_count)
            self.assertEqual(visible_sessions, page._sessions)
            self.assertNotEqual("（暂无会话）", page._list.item(0).text())
        finally:
            page.close()
            page.deleteLater()


class StartupSplashTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_startup_splash_is_visible_and_reports_the_current_stage(self) -> None:
        module = importlib.import_module("widget.ui.startup_splash")
        splash = module.StartupSplash(load_app_icon())
        try:
            splash.show()
            splash.set_stage("正在连接现有微信…")
            self.app.processEvents()
            self.assertTrue(splash.isVisible())
            self.assertEqual("正在连接现有微信…", splash.stage)
        finally:
            splash.close()
            splash.deleteLater()


class ApplicationIconTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_project_icon_loads_for_window_and_status_tray(self) -> None:
        self.assertTrue(project_icon_path().is_file())
        self.assertEqual(".svg", project_icon_path().suffix.lower())
        self.assertNotEqual("icon.png", project_icon_path().name.lower())
        self.assertTrue(windows_icon_path().is_file())
        self.assertEqual(".ico", windows_icon_path().suffix.lower())
        icon = load_app_icon()
        self.assertFalse(icon.isNull())
        self.assertFalse(QIcon(str(windows_icon_path())).isNull())
        self.assertFalse(status_icon(icon, "#21A366").isNull())

    def test_brand_mark_uses_the_same_application_icon(self) -> None:
        icon = load_app_icon()
        mark = brand_mark_pixmap(icon, 38)

        self.assertFalse(mark.isNull())
        self.assertEqual(38, mark.width())
        self.assertEqual(38, mark.height())

    def test_tray_keeps_project_icon_when_health_changes(self) -> None:
        tray = Tray(object(), lambda: None, lambda: None, app_icon=load_app_icon())
        tray.set_health(ok=False, degraded=True)
        self.assertFalse(tray.icon.icon().isNull())
        self.assertEqual("AI客服", tray.icon.toolTip())

    @unittest.skipUnless(os.name == "nt", "Windows native icon slots")
    def test_native_window_has_big_and_small_project_icons(self) -> None:
        import ctypes

        if QGuiApplication.platformName() != "windows":
            self.skipTest("requires the real Windows Qt platform, not offscreen")

        window = QWidget()
        window.show()
        self.app.processEvents()
        apply_windows_window_icon(window, load_app_icon())
        hwnd = int(window.winId())
        user32 = ctypes.windll.user32
        user32.SendMessageW.restype = ctypes.c_ssize_t
        self.assertTrue(user32.SendMessageW(hwnd, 0x007F, 1, 0))  # ICON_BIG
        self.assertTrue(user32.SendMessageW(hwnd, 0x007F, 2, 0) or
                        user32.SendMessageW(hwnd, 0x007F, 0, 0))   # ICON_SMALL2/SMALL
        window.close()


if __name__ == "__main__":
    unittest.main()
