from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from widget.app import build_pipeline
from widget.config import NotificationConfig, WidgetConfig, load_config
from widget.handoff import HandoffController
from widget.inbound import InboundFilter
from widget.notifications import NotificationGate
from widget.pipeline import Pipeline
from widget.scope import conversation_key, should_handle
from widget.state import RuntimeState
from widget.models import SendResult


def _msg(*, msg_id: str = "m1", channel: str = "douyin#shop_a",
         contact: str = "wxid_customer") -> dict:
    return {
        "channel": channel,
        "msg_id": msg_id,
        "contact_id": contact,
        "sender_id": contact,
        "text": "请问怎么配送？",
        "is_group": False,
        "at_me": False,
        "timestamp": 1,
    }


class _Bridge:
    def __init__(self, result: dict):
        self.result = result
        self.calls = 0
        self.delivery_updates: list[tuple[int, str]] = []

    def chat(self, _msg, conversation_id=None):
        self.calls += 1
        return dict(self.result)

    def mark_delivery(
        self, message_id: int, delivery_status: str, *, attempt_id: str = "",
    ) -> dict:
        self.delivery_updates.append((message_id, delivery_status))
        return {
            "message_id": message_id, "delivery_status": delivery_status,
            "changed": True, "attempt_id": attempt_id,
            "recovered_expired_lease": False,
        }


class _RecoveringBridge(_Bridge):
    def chat(self, _msg, conversation_id=None, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            from widget.bridge import BridgeError

            raise BridgeError("response lost after backend completed")
        return dict(self.result)


class _ProcessingBridge(_Bridge):
    def chat(self, _msg, conversation_id=None, **_kwargs):
        self.calls += 1
        if self.calls < 3:
            return {
                "action": "processing", "reply_text": "", "conversation_id": 7,
                "outbound_message_id": 0, "deduplicated": True,
            }
        return dict(self.result)


class _Sender:
    def __init__(self, ok: bool = True):
        self.calls = 0
        self.sent: list[tuple[str, str]] = []
        self.ok = ok

    def deliver(self, contact: str, text: str, reply_to=None) -> bool:
        self.calls += 1
        self.sent.append((contact, text, reply_to))
        return self.ok


class _UncertainSender(_Sender):
    def __init__(self):
        super().__init__(ok=False)
        self.last_result = SimpleNamespace(uncertain=True)


class _HandoffAdapter:
    def __init__(self):
        self.sent = []
        self.sent_messages = []

    def send_message(self, contact: str, text: str, provenance: str):
        self.sent_messages.append((contact, text, provenance))
        return SimpleNamespace(ok=provenance == "human")

    def send_reply(self, contact: str, text: str, reply_to: dict, provenance: str):
        self.sent.append((contact, text, reply_to, provenance))
        return SimpleNamespace(ok=provenance == "human")


class _HandoffHub:
    def __init__(self):
        self.adapter_instance = _HandoffAdapter()
        self.pipeline_instance = SimpleNamespace(release_contact=lambda _contact: None)

    def adapter(self, _channel=None):
        return self.adapter_instance

    def pipeline(self, _channel=None):
        return self.pipeline_instance


class MessagePolicyTests(unittest.TestCase):
    def test_auto_send_is_disabled_by_default(self):
        self.assertFalse(WidgetConfig().auto_send)

    def test_channel_conversation_blocklist_stops_before_backend(self):
        cfg = WidgetConfig()
        cfg.scope.private_mode = "all"
        cfg.scope.conversation_blocklist.append(
            conversation_key("douyin#shop_a", "wxid_customer")
        )
        bridge = _Bridge({"action": "handoff", "reply_text": "人工"})
        pipe = Pipeline(cfg, InboundFilter(), bridge, _Sender(), "wxid_self")

        self.assertEqual("ignored", pipe.handle(_msg()))
        self.assertEqual(0, bridge.calls)
        self.assertFalse(should_handle(_msg(), cfg.scope))

    def test_selected_mode_only_accepts_explicit_customer(self):
        cfg = WidgetConfig()
        cfg.scope.private_mode = "selected"
        self.assertFalse(should_handle(_msg(), cfg.scope))
        cfg.scope.conversation_allowlist.append(
            conversation_key("douyin#shop_a", "wxid_customer")
        )
        self.assertTrue(should_handle(_msg(), cfg.scope))

    def test_selected_mode_also_requires_explicit_group_conversation(self):
        cfg = WidgetConfig()
        cfg.scope.private_mode = "selected"
        cfg.scope.allow_group = True
        cfg.scope.group_trigger = "all"
        group = _msg(contact="customer_group@chatroom")
        group["is_group"] = True

        self.assertFalse(should_handle(group, cfg.scope))
        cfg.scope.conversation_allowlist.append(
            conversation_key("douyin#shop_a", "customer_group@chatroom")
        )
        self.assertTrue(should_handle(group, cfg.scope))

    def test_answerable_message_is_quiet_draft_when_auto_send_is_off(self):
        cfg = WidgetConfig(auto_send=False)
        cfg.scope.private_mode = "all"
        bridge = _Bridge({
            "action": "auto_reply", "reply_text": "支持同城配送。", "conversation_id": 7,
            "outbound_message_id": 70,
        })
        sender = _Sender()
        pending = []
        pipe = Pipeline(cfg, InboundFilter(), bridge, sender, "wxid_self",
                        on_pending=lambda m, r: pending.append((m, r)))

        self.assertEqual("auto_reply_draft", pipe.handle(_msg()))
        self.assertEqual(0, sender.calls)
        self.assertEqual("auto_reply_draft", pending[0][1]["pending_kind"])
        self.assertEqual([(70, "draft")], bridge.delivery_updates)

    def test_handoff_sends_notice_and_remains_pending(self):
        cfg = WidgetConfig(auto_send=True)
        cfg.scope.private_mode = "all"
        bridge = _Bridge({
            "action": "handoff",
            "reply_text": "帮您转接人工客服回复中，请稍等",
            "outbound_message_id": 71,
        })
        sender = _Sender()
        pending = []
        pipe = Pipeline(cfg, InboundFilter(), bridge, sender, "wxid_self",
                        on_pending=lambda m, r: pending.append((m, r)))

        self.assertEqual("handoff", pipe.handle(_msg()))
        self.assertEqual(
            [("wxid_customer", "帮您转接人工客服回复中，请稍等", None)],
            sender.sent,
        )
        self.assertEqual(1, len(pending))
        self.assertEqual("handoff_notified", pending[0][1]["record_action"])

    def test_handoff_is_pending_but_not_sent_when_auto_send_is_off(self):
        cfg = WidgetConfig(auto_send=False)
        cfg.scope.private_mode = "all"
        sender = _Sender()
        pending = []
        pipe = Pipeline(
            cfg,
            InboundFilter(),
            _Bridge({
                "action": "handoff", "reply_text": cfg.handoff_reply,
                "outbound_message_id": 72,
            }),
            sender,
            "wxid_self",
            on_pending=lambda m, r: pending.append((m, r)),
        )

        self.assertEqual("handoff", pipe.handle(_msg()))
        self.assertEqual([], sender.sent)
        self.assertEqual(1, len(pending))

    def test_failed_handoff_notice_still_remains_pending(self):
        cfg = WidgetConfig(auto_send=True)
        cfg.scope.private_mode = "all"
        sender = _Sender(ok=False)
        pending = []
        pipe = Pipeline(
            cfg,
            InboundFilter(),
            _Bridge({
                "action": "handoff", "reply_text": cfg.handoff_reply,
                "outbound_message_id": 73,
            }),
            sender,
            "wxid_self",
            on_pending=lambda m, r: pending.append((m, r)),
        )

        self.assertEqual("handoff", pipe.handle(_msg()))
        self.assertEqual(1, len(sender.sent))
        self.assertEqual(1, len(pending))
        self.assertNotIn("record_action", pending[0][1])

    def test_uncertain_auto_send_keeps_sending_lease_for_reconciliation(self):
        cfg = WidgetConfig(auto_send=True)
        cfg.scope.private_mode = "all"
        sender = _UncertainSender()
        bridge = _Bridge({
            "action": "auto_reply", "reply_text": "今天发货",
            "outbound_message_id": 74,
        })
        pending = []
        pipe = Pipeline(
            cfg, InboundFilter(), bridge, sender, "self",
            on_pending=lambda message, result: pending.append((message, result)),
        )

        self.assertEqual("delivery_uncertain", pipe.handle(_msg()))
        self.assertEqual([(74, "sending")], bridge.delivery_updates)
        self.assertTrue(pending[0][1]["delivery_uncertain"])
        self.assertEqual("delivery_uncertain", pending[0][1]["pending_kind"])

    def test_uncertain_auto_send_confirmed_by_immediate_history_does_not_enter_handoff(self):
        cfg = WidgetConfig(auto_send=True)
        cfg.scope.private_mode = "all"
        sender = _UncertainSender()
        sender.reconcile_delivery = lambda _contact, _text, _since: "delivered"
        bridge = _Bridge({
            "action": "auto_reply",
            "intent": "chitchat",
            "reply_text": "麻将这名字真有趣",
            "outbound_message_id": 174,
        })
        auto = []
        pending = []
        pipe = Pipeline(
            cfg,
            InboundFilter(),
            bridge,
            sender,
            "self",
            on_auto=lambda message, result: auto.append((message, result)),
            on_pending=lambda message, result: pending.append((message, result)),
        )

        message = _msg()
        message["text"] = "我有一只小狗叫麻将"
        self.assertEqual("auto_reply", pipe.handle(message))
        self.assertEqual([(174, "sending"), (174, "delivered")], bridge.delivery_updates)
        self.assertEqual(1, len(auto))
        self.assertEqual([], pending)

    def test_handoff_with_a_fresh_sending_lease_stays_pending_for_reconciliation(self):
        cfg = WidgetConfig(auto_send=True)
        cfg.scope.private_mode = "all"

        class HeldLeaseBridge(_Bridge):
            def mark_delivery(self, message_id, delivery_status, *, attempt_id=""):
                if delivery_status == "sending":
                    return {
                        "message_id": message_id,
                        "delivery_status": "sending",
                        "changed": False,
                        "attempt_id": "reconciliation-owner",
                        "recovered_expired_lease": False,
                    }
                return super().mark_delivery(
                    message_id, delivery_status, attempt_id=attempt_id,
                )

        sender = _Sender()
        bridge = HeldLeaseBridge({
            "action": "handoff",
            "reply_text": cfg.handoff_reply,
            "outbound_message_id": 75,
            "delivery_status": "sending",
            "deduplicated": True,
        })
        pipe = Pipeline(cfg, InboundFilter(), bridge, sender, "self")

        self.assertEqual("delivery_waiting", pipe.handle(_msg()))
        self.assertEqual([], sender.sent)
        self.assertEqual([], bridge.delivery_updates)

    def test_default_handoff_notice_matches_customer_copy(self):
        self.assertEqual(
            "帮您转接人工客服回复中，请稍等",
            WidgetConfig().handoff_reply,
        )

    def test_pending_messages_remain_independent_per_inbound_message(self):
        state = RuntimeState(WidgetConfig())
        state.add_pending(_msg(msg_id="m1"), {"reply_text": "草稿1"})
        second = _msg(msg_id="m2")
        second["text"] = "第二条"
        state.add_pending(second, {"reply_text": "草稿2"})

        self.assertEqual(2, len(state.pending))
        self.assertEqual(["m1", "m2"], [item["msg_id"] for item in state.pending])
        self.assertEqual(["请问怎么配送？", "第二条"], [item["text"] for item in state.pending])

    def test_pending_same_message_updates_kind_instead_of_duplicating(self):
        state = RuntimeState(WidgetConfig())
        message = _msg()
        self.assertTrue(state.add_pending(
            message, {"reply_text": "草稿", "pending_kind": "delivery_uncertain"},
        ))
        self.assertFalse(state.add_pending(
            message, {"reply_text": "新草稿", "pending_kind": "handoff"},
        ))
        self.assertEqual(1, len(state.pending))
        self.assertEqual("handoff", state.pending[0]["kind"])
        self.assertEqual("新草稿", state.pending[0]["draft"])

    def test_delivery_replay_bypasses_only_in_process_duplicate_filter(self):
        inbound = InboundFilter()
        message = _msg()
        self.assertTrue(inbound.accept(message, "self"))
        self.assertFalse(inbound.accept(message, "self"))
        replay = dict(message, delivery_replay=True)
        self.assertTrue(inbound.accept(replay, "self"))

    def test_uncertain_auto_reply_recovery_removes_exact_pending_without_recounting(self):
        cfg = WidgetConfig(
            auto_send=True, send_delay_min_s=0, send_delay_max_s=0,
        )
        cfg.scope.private_mode = "all"
        state = RuntimeState(cfg)
        reconciliation = iter(["unknown", "delivered"])

        class Adapter:
            channel = "douyin#shop_a"

            def self_wxid(self):
                return "self"

            def send_message(self, _contact, _text, provenance="ai"):
                return SendResult(ok=False, uncertain=True, error="unknown")

            def reconcile_delivery(self, _contact, _text, _since):
                return next(reconciliation)

        class RecoveryBridge(_Bridge):
            sending_calls = 0

            def mark_delivery(self, message_id, delivery_status, *, attempt_id=""):
                self.delivery_updates.append((message_id, delivery_status))
                if delivery_status == "sending":
                    self.sending_calls += 1
                return {
                    "message_id": message_id,
                    "delivery_status": delivery_status,
                    "changed": True,
                    "attempt_id": attempt_id,
                    "recovered_expired_lease": (
                        delivery_status == "sending" and self.sending_calls > 1
                    ),
                }

        bridge = RecoveryBridge({
            "action": "auto_reply", "reply_text": "今天发货",
            "outbound_message_id": 176,
        })
        pipe = build_pipeline(cfg, Adapter(), bridge, state)
        message = _msg()

        self.assertEqual("delivery_uncertain", pipe.handle(message))
        self.assertEqual("delivery_uncertain", state.pending[0]["kind"])
        self.assertEqual((1, 0), state.today_counts())

        replay = dict(message, delivery_replay=True)
        self.assertEqual("auto_reply", pipe.handle(replay))
        self.assertEqual([], state.pending)
        self.assertEqual((1, 1), state.today_counts())

    def test_delivery_waiting_rebuilds_visible_pending_after_restart(self):
        cfg = WidgetConfig(auto_send=True)
        cfg.scope.private_mode = "all"
        state = RuntimeState(cfg)

        class Adapter:
            channel = "douyin#shop_a"

            def self_wxid(self):
                return "self"

        class HeldLeaseBridge(_Bridge):
            def mark_delivery(self, message_id, delivery_status, *, attempt_id=""):
                return {
                    "message_id": message_id,
                    "delivery_status": "sending",
                    "changed": False,
                    "attempt_id": "prior-owner",
                    "recovered_expired_lease": False,
                }

        pipe = build_pipeline(
            cfg,
            Adapter(),
            HeldLeaseBridge({
                "action": "auto_reply", "reply_text": "处理中",
                "outbound_message_id": 177, "delivery_status": "sending",
            }),
            state,
        )
        replay = dict(_msg(), delivery_replay=True)

        self.assertEqual("delivery_waiting", pipe.handle(replay))
        self.assertEqual("delivery_waiting", state.pending[0]["kind"])
        self.assertEqual((1, 0), state.today_counts())

    def test_delivery_replay_reconciles_before_changed_auto_send_policy(self):
        cfg = WidgetConfig(auto_send=False)
        cfg.scope.private_mode = "all"
        sender = _Sender()
        sender.reconcile_delivery = lambda _contact, _text, _since: "delivered"

        class RecoveredBridge(_Bridge):
            def mark_delivery(self, message_id, delivery_status, *, attempt_id=""):
                self.delivery_updates.append((message_id, delivery_status))
                return {
                    "message_id": message_id,
                    "delivery_status": delivery_status,
                    "changed": True,
                    "attempt_id": attempt_id,
                    "recovered_expired_lease": delivery_status == "sending",
                }

        auto = []
        pending = []
        pipe = Pipeline(
            cfg,
            InboundFilter(),
            RecoveredBridge({
                "action": "auto_reply", "reply_text": "已发送过",
                "outbound_message_id": 178, "delivery_status": "sending",
            }),
            sender,
            "self",
            on_auto=lambda message, result: auto.append((message, result)),
            on_pending=lambda message, result: pending.append((message, result)),
            is_ai_enabled=lambda: False,
        )
        replay = dict(_msg(), delivery_replay=True)

        self.assertEqual("auto_reply", pipe.handle(replay))
        self.assertEqual([], sender.sent)
        self.assertEqual(1, len(auto))
        self.assertEqual([], pending)

    def test_delivery_replay_not_delivered_respects_changed_auto_send_policy(self):
        cfg = WidgetConfig(auto_send=False)
        cfg.scope.private_mode = "all"
        sender = _Sender()
        sender.reconcile_delivery = lambda _contact, _text, _since: "not_delivered"

        class RecoveredBridge(_Bridge):
            def mark_delivery(self, message_id, delivery_status, *, attempt_id=""):
                self.delivery_updates.append((message_id, delivery_status))
                return {
                    "message_id": message_id,
                    "delivery_status": delivery_status,
                    "changed": True,
                    "attempt_id": attempt_id,
                    "recovered_expired_lease": delivery_status == "sending",
                }

        pending = []
        bridge = RecoveredBridge({
            "action": "auto_reply", "reply_text": "不再自动发送",
            "outbound_message_id": 179, "delivery_status": "sending",
        })
        pipe = Pipeline(
            cfg, InboundFilter(), bridge, sender, "self",
            on_pending=lambda message, result: pending.append((message, result)),
        )

        self.assertEqual(
            "auto_reply_draft", pipe.handle(dict(_msg(), delivery_replay=True)),
        )
        self.assertEqual([], sender.sent)
        self.assertEqual("auto_reply_draft", pending[0][1]["pending_kind"])
        self.assertEqual(
            [(179, "sending"), (179, "failed"), (179, "draft")],
            bridge.delivery_updates,
        )

    def test_sent_handoff_notice_is_counted_as_an_outbound_message(self):
        state = RuntimeState(WidgetConfig())

        state.record_inbound(_msg(), "handoff_notified")

        self.assertEqual((1, 1), state.today_counts())
        self.assertEqual(1, state.sent_by_channel["douyin#shop_a"])

    def test_manual_reply_removes_pending_and_notifies_navigation(self):
        state = RuntimeState(WidgetConfig())
        state.add_pending(_msg(), {"reply_text": "draft"})
        changes = []
        controller = HandoffController(
            _HandoffHub(), state, on_changed=lambda: changes.append(len(state.pending))
        )

        pending_id = state.pending[0]["id"]
        self.assertTrue(controller.reply_pending(pending_id, "已人工回复"))
        self.assertEqual([], state.pending)
        self.assertEqual([0], changes)

    def test_manual_reply_can_defer_navigation_notification_to_gui_thread(self):
        state = RuntimeState(WidgetConfig())
        state.add_pending(_msg(), {"reply_text": "draft"})
        changes = []
        controller = HandoffController(
            _HandoffHub(), state, on_changed=lambda: changes.append(len(state.pending))
        )

        pending_id = state.pending[0]["id"]
        self.assertTrue(
            controller.reply_pending(pending_id, "已人工回复", notify=False)
        )
        self.assertEqual([], state.pending)
        self.assertEqual([], changes)

        controller.notify_changed()
        self.assertEqual([0], changes)

    def test_manual_reply_exposes_channel_failure_reason_and_keeps_pending(self):
        state = RuntimeState(WidgetConfig())
        state.add_pending(_msg(), {"reply_text": "draft"})
        hub = _HandoffHub()
        hub.adapter_instance.send_reply = lambda *_args, **_kwargs: SimpleNamespace(
            ok=False,
            error="该抖音账号未启用真实发送（send_enabled=false）",
        )
        controller = HandoffController(hub, state)

        self.assertFalse(controller.reply_pending(state.pending[0]["id"], "人工回复"))
        self.assertIn("send_enabled=false", controller.last_error)
        self.assertEqual(1, len(state.pending))

    def test_manual_reply_is_blocked_while_delivery_is_uncertain(self):
        state = RuntimeState(WidgetConfig())
        state.add_pending(
            _msg(), {"reply_text": "可能已发送", "pending_kind": "delivery_uncertain"},
        )
        hub = _HandoffHub()
        controller = HandoffController(hub, state)

        self.assertFalse(controller.reply_pending(state.pending[0]["id"], "不要重复发送"))
        self.assertEqual([], hub.adapter_instance.sent)
        self.assertEqual(1, len(state.pending))

    def test_conversation_send_is_blocked_while_delivery_is_waiting(self):
        state = RuntimeState(WidgetConfig())
        state.add_pending(
            _msg(), {"reply_text": "正在对账", "pending_kind": "delivery_waiting"},
        )
        hub = _HandoffHub()
        controller = HandoffController(hub, state)

        self.assertFalse(controller.send("wxid_customer", "普通会话入口", "douyin#shop_a"))
        self.assertEqual([], hub.adapter_instance.sent_messages)

    def test_replying_one_pending_message_keeps_other_messages_for_same_customer(self):
        state = RuntimeState(WidgetConfig())
        first = _msg(msg_id="m1")
        second = _msg(msg_id="m2")
        second["text"] = "第二个问题"
        state.add_pending(first, {"reply_text": "草稿1"})
        state.add_pending(second, {"reply_text": "草稿2"})
        controller = HandoffController(_HandoffHub(), state)
        live_events = []
        state.set_conversation_listener(live_events.append)

        self.assertTrue(controller.reply_pending(state.pending[1]["id"], "第二题答复"))

        self.assertEqual(["m1"], [item["msg_id"] for item in state.pending])
        sent = controller.hub.adapter_instance.sent
        self.assertEqual(1, len(sent))
        self.assertEqual(("wxid_customer", "第二题答复", "human"),
                         (sent[0][0], sent[0][1], sent[0][3]))
        self.assertEqual("m2", sent[0][2]["msg_id"])
        self.assertEqual("第二个问题", sent[0][2]["text"])
        self.assertEqual("wxid_customer", live_events[0]["contact_id"])
        self.assertIn("第二个问题", live_events[0]["text"])

    def test_manual_message_outside_pending_queue_is_not_quoted(self):
        hub = _HandoffHub()
        controller = HandoffController(hub, RuntimeState(WidgetConfig()))

        self.assertTrue(controller.send("wxid_customer", "普通人工消息"))

        self.assertEqual([], hub.adapter_instance.sent)
        self.assertEqual(
            [("wxid_customer", "普通人工消息", "human")],
            hub.adapter_instance.sent_messages,
        )

    def test_ai_reply_does_not_quote_the_inbound_message(self):
        cfg = WidgetConfig(auto_send=True)
        cfg.scope.private_mode = "all"
        sender = _Sender()
        pipe = Pipeline(
            cfg,
            InboundFilter(),
            _Bridge({
                "action": "auto_reply", "reply_text": "第二题的答案",
                "outbound_message_id": 81,
            }),
            sender,
            "wxid_self",
        )
        inbound = _msg(msg_id="m2")
        inbound["text"] = "这是第二个问题"

        self.assertEqual("auto_reply", pipe.handle(inbound))
        self.assertEqual(
            [("wxid_customer", "第二题的答案", None)],
            sender.sent,
        )
        self.assertEqual([(81, "sending"), (81, "delivered")], pipe.bridge.delivery_updates)

    def test_failed_ai_send_is_marked_failed_and_never_confirmed_delivered(self):
        cfg = WidgetConfig(auto_send=True)
        cfg.scope.private_mode = "all"
        bridge = _Bridge({
            "action": "auto_reply", "reply_text": "支持同城配送。",
            "outbound_message_id": 82,
        })
        pending = []
        pipe = Pipeline(
            cfg, InboundFilter(), bridge, _Sender(ok=False), "wxid_self",
            on_pending=lambda m, r: pending.append((m, r)),
        )

        self.assertEqual("handoff", pipe.handle(_msg()))
        self.assertEqual([(82, "sending"), (82, "failed")], bridge.delivery_updates)
        self.assertEqual(1, len(pending))

    def test_backend_duplicate_result_is_not_sent_again(self):
        cfg = WidgetConfig(auto_send=True)
        cfg.scope.private_mode = "all"
        sender = _Sender()
        pipe = Pipeline(
            cfg,
            InboundFilter(),
            _Bridge({
                "action": "duplicate", "reply_text": "", "conversation_id": 7,
                "outbound_message_id": 82, "deduplicated": True,
            }),
            sender,
            "wxid_self",
        )

        self.assertEqual("ignored", pipe.handle(_msg()))
        self.assertEqual([], sender.sent)

    def test_lost_first_response_recovers_pending_reply_without_second_generation(self):
        cfg = WidgetConfig(auto_send=True)
        cfg.scope.private_mode = "all"
        sender = _Sender()
        bridge = _RecoveringBridge({
            "action": "auto_reply", "reply_text": "恢复投递的回答",
            "conversation_id": 7, "outbound_message_id": 83,
            "deduplicated": True,
        })
        pipe = Pipeline(cfg, InboundFilter(), bridge, sender, "wxid_self")

        self.assertEqual("auto_reply", pipe.handle(_msg()))
        self.assertEqual(2, bridge.calls)
        self.assertEqual([("wxid_customer", "恢复投递的回答", None)], sender.sent)
        self.assertEqual([(83, "sending"), (83, "delivered")], bridge.delivery_updates)

    def test_overlapping_retry_polls_until_first_request_finishes_generation(self):
        cfg = WidgetConfig(auto_send=True)
        cfg.scope.private_mode = "all"
        sleeps = []
        sender = _Sender()
        bridge = _ProcessingBridge({
            "action": "auto_reply", "reply_text": "稍后完成的回答",
            "conversation_id": 7, "outbound_message_id": 84,
            "deduplicated": True, "delivery_status": "pending",
        })
        pipe = Pipeline(
            cfg, InboundFilter(), bridge, sender, "wxid_self",
            recovery_sleep=sleeps.append, recovery_attempts=4,
        )

        self.assertEqual("auto_reply", pipe.handle(_msg()))
        self.assertEqual(3, bridge.calls)
        self.assertEqual([1.0, 1.0], sleeps)
        self.assertEqual([("wxid_customer", "稍后完成的回答", None)], sender.sent)

    def test_processing_recovery_uses_short_deadline_bounded_probes(self):
        cfg = WidgetConfig(auto_send=True)
        cfg.scope.private_mode = "all"
        clock = [0.0]
        timeouts = []

        class NeverReadyBridge(_Bridge):
            def chat(self, _msg, conversation_id=None, **kwargs):
                self.calls += 1
                timeouts.append(kwargs.get("timeout"))
                if self.calls > 1:
                    clock[0] += float(kwargs.get("timeout") or 0)
                return {"action": "processing", "conversation_id": 7}

        bridge = NeverReadyBridge({})
        pending = []
        pipe = Pipeline(
            cfg, InboundFilter(), bridge, _Sender(), "wxid_self",
            on_pending=lambda m, r: pending.append((m, r)),
            recovery_sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
            recovery_clock=lambda: clock[0], recovery_budget_s=6,
            recovery_attempts=20,
        )

        self.assertEqual("error", pipe.handle(_msg()))
        self.assertEqual([None, 5.0], timeouts)
        self.assertLessEqual(clock[0], 6.0)

    def test_expired_lease_reconciles_local_history_before_resend(self):
        cfg = WidgetConfig(auto_send=True)
        cfg.scope.private_mode = "all"
        bridge = _Bridge({
            "action": "auto_reply", "reply_text": "已经在抖音私信里",
            "conversation_id": 7, "outbound_message_id": 86,
            "delivery_status": "sending", "deduplicated": True,
        })
        sender = _Sender()
        sender.was_delivered_since = lambda _contact, _text, _since: True
        original_mark = bridge.mark_delivery

        def takeover_mark(message_id, delivery_status, *, attempt_id=""):
            response = original_mark(
                message_id, delivery_status, attempt_id=attempt_id,
            )
            if delivery_status == "sending":
                response["recovered_expired_lease"] = True
            return response

        bridge.mark_delivery = takeover_mark
        pipe = Pipeline(cfg, InboundFilter(), bridge, sender, "wxid_self")

        self.assertEqual("auto_reply", pipe.handle(_msg()))
        self.assertEqual([], sender.sent)
        self.assertEqual([(86, "sending"), (86, "delivered")], bridge.delivery_updates)

    def test_fresh_claim_does_not_use_same_text_history_to_skip_send(self):
        cfg = WidgetConfig(auto_send=True)
        cfg.scope.private_mode = "all"
        bridge = _Bridge({
            "action": "auto_reply", "reply_text": "固定问候",
            "conversation_id": 7, "outbound_message_id": 87,
            "delivery_status": "pending",
        })
        sender = _Sender()
        sender.was_delivered_since = lambda _contact, _text, _since: True
        pipe = Pipeline(cfg, InboundFilter(), bridge, sender, "wxid_self")

        self.assertEqual("auto_reply", pipe.handle(_msg()))
        self.assertEqual([("wxid_customer", "固定问候", None)], sender.sent)

    def test_two_delivery_attempts_only_send_once_when_second_cannot_claim(self):
        cfg = WidgetConfig(auto_send=True)
        cfg.scope.private_mode = "all"
        sender = _Sender()

        class SingleClaimBridge(_Bridge):
            claimed = False

            def mark_delivery(self, message_id, delivery_status, *, attempt_id=""):
                if delivery_status == "sending":
                    if self.claimed:
                        return {
                            "delivery_status": "sending", "changed": False,
                            "attempt_id": "first-owner",
                        }
                    self.claimed = True
                self.delivery_updates.append((message_id, delivery_status))
                return {
                    "delivery_status": delivery_status, "changed": True,
                    "attempt_id": attempt_id,
                }

        bridge = SingleClaimBridge({
            "action": "auto_reply", "reply_text": "只应发送一次",
            "conversation_id": 7, "outbound_message_id": 85,
        })
        first = Pipeline(cfg, InboundFilter(), bridge, sender, "wxid_self")
        second = Pipeline(cfg, InboundFilter(), bridge, sender, "wxid_self")

        self.assertEqual("auto_reply", first.handle(_msg(msg_id="shared")))
        self.assertEqual("delivery_waiting", second.handle(_msg(msg_id="shared")))
        self.assertEqual(1, len(sender.sent))

    def test_live_events_quote_only_manual_pending_replies(self):
        state = RuntimeState(WidgetConfig())
        events = []
        state.set_conversation_listener(events.append)
        inbound = _msg()

        state.publish_outbound(inbound, "AI 自动回答", "ai")
        state.publish_outbound(inbound, "人工待办回答", "agent", quote=True)

        self.assertEqual("AI 自动回答", events[0]["text"])
        self.assertNotIn("请问怎么配送", events[0]["text"])
        self.assertIn("人工待办回答", events[1]["text"])
        self.assertIn("请问怎么配送", events[1]["text"])

    def test_notification_gate_only_notifies_handoff_and_applies_cooldown(self):
        now = [100.0]
        cfg = NotificationConfig(handoff=True, auto_reply=False, cooldown_s=30)
        gate = NotificationGate(cfg, clock=lambda: now[0])

        self.assertFalse(gate.should_notify("auto_reply", "douyin", "customer"))
        self.assertTrue(gate.should_notify("handoff", "douyin", "customer"))
        self.assertFalse(gate.should_notify("handoff", "douyin", "customer"))
        now[0] += 31
        self.assertTrue(gate.should_notify("handoff", "douyin", "customer"))

    def test_conversation_and_notification_choices_are_persisted(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "widget.yaml"
            state = RuntimeState(WidgetConfig(), config_path=path)
            state.set_conversation_enabled("douyin", "customer", True)
            state.set_notifications(auto_reply=True, bring_to_front=True)

            loaded = load_config(path)
            self.assertIn("douyin|customer", loaded.scope.conversation_allowlist)
            self.assertTrue(loaded.notifications.auto_reply)
            self.assertTrue(loaded.notifications.bring_to_front)


if __name__ == "__main__":
    unittest.main()
