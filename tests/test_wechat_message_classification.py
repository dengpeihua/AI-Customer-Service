from __future__ import annotations

import unittest

from widget.adapters.wechat_hook import WeChatHookAdapter
from widget.config import WidgetConfig
from widget.inbound import InboundFilter


def _adapter() -> WeChatHookAdapter:
    return WeChatHookAdapter(
        WidgetConfig(), patch_login=False,
        self_wxid_override="profile_id_that_differs_from_message_db",
    )


class WeChatMessageClassificationTests(unittest.TestCase):
    def test_manual_private_message_is_inferred_as_self(self) -> None:
        adapter = _adapter()
        adapter._fts_id2name = {1: "wxid_customer", 2: "local_self_alias"}

        msg = adapter._build_fts_msg("message_fts_v4_0", {
            "local_type": 1, "session_id": 1, "sender_id": 2,
            "message_local_id": 7, "create_time": 1, "acontent": "manual reply",
        })

        self.assertIsNone(msg)
        self.assertIn("local_self_alias", adapter._self_wxid_aliases)

    def test_inferred_self_alias_filters_manual_group_message(self) -> None:
        adapter = _adapter()
        adapter._fts_id2name = {
            1: "wxid_customer", 2: "local_self_alias", 3: "123@chatroom",
        }
        adapter._is_self_sender("local_self_alias", "wxid_customer")

        msg = adapter._build_fts_msg("message_fts_v4_0", {
            "local_type": 1, "session_id": 3, "sender_id": 2,
            "message_local_id": 8, "create_time": 2, "acontent": "group reply",
        })

        self.assertIsNone(msg)

    def test_incoming_group_message_is_preserved(self) -> None:
        adapter = _adapter()
        adapter._fts_id2name = {3: "123@chatroom", 4: "wxid_member"}

        msg = adapter._build_fts_msg("message_fts_v4_0", {
            "local_type": 1, "session_id": 3, "sender_id": 4,
            "message_local_id": 9, "create_time": 3,
            "acontent": "wxid_member:\ngroup question",
        })

        self.assertIsNotNone(msg)
        self.assertTrue(msg["is_group"])
        self.assertEqual("123@chatroom", msg["contact_id"])
        self.assertEqual("wxid_member", msg["sender_id"])
        self.assertEqual("group question", msg["text"])

    def test_session_list_keeps_groups_and_excludes_official_system_accounts(self) -> None:
        adapter = _adapter()
        adapter._query_db = lambda _db, _sql: [
            {"username": "wxid_customer", "summary": "private", "sort_timestamp": 4},
            {"username": "123@chatroom", "summary": "group", "sort_timestamp": 3},
            {"username": "gh_official", "summary": "official", "sort_timestamp": 2},
            {"username": "brandservicesessionholder", "summary": "official", "sort_timestamp": 1},
            {"username": "filehelper", "summary": "system", "sort_timestamp": 0},
        ]

        self.assertEqual(
            ["wxid_customer", "123@chatroom"],
            [row["wxid"] for row in adapter.list_sessions()],
        )

    def test_inbound_filter_rejects_official_holder_even_without_gh_sender(self) -> None:
        msg = {
            "channel": "wechat_personal", "msg_id": "m1",
            "contact_id": "brandservicesessionholder", "sender_id": "unknown_sender",
            "text": "subscription update", "is_group": False,
            "at_me": False, "timestamp": 1,
        }

        self.assertFalse(InboundFilter().accept(msg, "wxid_self"))


if __name__ == "__main__":
    unittest.main()
