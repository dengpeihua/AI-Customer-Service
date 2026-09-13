from __future__ import annotations

import unittest


class LiveConversationEventTests(unittest.TestCase):
    def test_backend_and_douyin_snapshots_are_merged_without_duplicates(self) -> None:
        from widget.live_conversation import merge_history_snapshots

        backend = [
            {"local_id": "backend:1", "text": "营业时间是什么", "is_self": False, "ts": 10},
            {"local_id": "backend:2", "text": "每天 9 点到 21 点", "is_self": True,
             "ts": 20, "provenance": "ai"},
        ]
        douyin = [
            {"local_id": "douyin:7", "text": "营业时间是什么", "is_self": False, "ts": 11},
        ]

        merged = merge_history_snapshots(douyin, backend)

        self.assertEqual(["营业时间是什么", "每天 9 点到 21 点"], [m["text"] for m in merged])
        self.assertEqual("douyin:7", merged[0]["local_id"])

    def test_snapshot_merge_is_independent_of_arrival_order(self) -> None:
        from widget.live_conversation import merge_history_snapshots

        backend = [{"local_id": "b:1", "text": "客户问题", "is_self": False, "ts": 10}]
        douyin = [{"local_id": "w:1", "text": "抖音私信里的人工回复", "is_self": True, "ts": 12}]

        left = merge_history_snapshots(douyin, backend)
        right = merge_history_snapshots(backend, douyin)

        self.assertEqual(
            [(m["text"], m["is_self"]) for m in left],
            [(m["text"], m["is_self"]) for m in right],
        )
    def test_live_inbound_is_visible_before_database_catches_up(self) -> None:
        from widget.live_conversation import event_to_history_message, merge_live_messages

        event = {
            "event_id": "in:m2",
            "direction": "inbound",
            "contact_id": "wxid_customer",
            "sender_id": "wxid_customer",
            "text": "刚刚收到的新消息",
            "timestamp": 20,
        }
        live = [event_to_history_message(event)]

        self.assertEqual("刚刚收到的新消息", merge_live_messages([], live)[0]["text"])

    def test_unknown_timestamp_snapshot_keeps_visual_order(self) -> None:
        from widget.live_conversation import merge_live_messages

        stored = [
            {"local_id": "z-id", "text": "视觉第一条", "kind": "text",
             "is_self": False, "ts": 0},
            {"local_id": "a-id", "text": "视觉第二条", "kind": "text",
             "is_self": True, "ts": 0},
        ]

        merged = merge_live_messages(stored, [])

        self.assertEqual(["视觉第一条", "视觉第二条"], [row["text"] for row in merged])

    def test_database_copy_replaces_matching_optimistic_event_without_duplicate(self) -> None:
        from widget.live_conversation import event_to_history_message, merge_live_messages

        event = {
            "event_id": "out:m2",
            "direction": "outbound",
            "contact_id": "wxid_customer",
            "text": "答复内容",
            "timestamp": 21,
            "provenance": "ai",
        }
        live = [event_to_history_message(event)]
        stored = [{"local_id": 99, "kind": "text", "text": "答复内容", "is_self": True, "ts": 22}]

        merged = merge_live_messages(stored, live)

        self.assertEqual(1, len(merged))
        self.assertEqual(99, merged[0]["local_id"])

    def test_quote_fallback_identifies_the_customer_message(self) -> None:
        from widget.reply_quote import quoted_reply_text

        self.assertEqual(
            "「回复：这是客户的第二个问题」\n这是对应答复",
            quoted_reply_text("这是对应答复", "这是客户的第二个问题"),
        )

    def test_identical_live_replies_are_matched_to_database_rows_one_to_one(self) -> None:
        from widget.live_conversation import event_to_history_message, merge_live_messages

        live = [event_to_history_message({
            "event_id": f"out:{index}", "direction": "outbound", "text": "收到",
            "timestamp": 20 + index,
        }) for index in range(2)]
        stored = [{"local_id": 1, "text": "收到", "is_self": True, "ts": 20}]

        merged = merge_live_messages(stored, live)

        self.assertEqual(2, len(merged))

    def test_live_event_preserves_douyin_rich_fields(self) -> None:
        from widget.live_conversation import event_to_history_message

        message = event_to_history_message({
            "event_id": "im:1",
            "direction": "inbound",
            "contact_id": "im:conversation-1",
            "sender_id": "im:conversation-1",
            "sender_name": "Alice",
            "sender_avatar": "https://img.example/avatar.jpg",
            "kind": "image",
            "text": "[图片]",
            "media_url": "https://img.example/photo.jpg",
            "description": "图片说明",
            "display_time": "昨天 12:00",
            "quote_sender": "Bob",
            "quote_text": "original",
            "timestamp": 1700000000,
        })

        self.assertEqual("quote", message["kind"])
        self.assertEqual("Alice", message["sender_name"])
        self.assertTrue(message["sender_avatar"].endswith("avatar.jpg"))
        self.assertTrue(message["media_url"].endswith("photo.jpg"))
        self.assertEqual("图片说明", message["description"])
        self.assertEqual("昨天 12:00", message["display_time"])
        self.assertEqual("original", message["quote_text"])


if __name__ == "__main__":
    unittest.main()
