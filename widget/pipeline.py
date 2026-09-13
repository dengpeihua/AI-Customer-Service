from __future__ import annotations
import time
import uuid
from typing import Callable
from widget.config import WidgetConfig
from widget.inbound import InboundFilter
from widget.scope import should_handle
from widget.bridge import Bridge, BridgeError
from widget.sender import Sender
from widget.models import InboundMsg

class Pipeline:
    def __init__(self, cfg: WidgetConfig, inbound_filter: InboundFilter, bridge: Bridge,
                 sender: Sender, self_wxid: str,
                 on_pending: Callable[[InboundMsg, dict], None] | None = None,
                 on_auto: Callable[[InboundMsg, dict], None] | None = None,
                 is_ai_enabled: Callable[[], bool] | None = None,
                 recovery_sleep: Callable[[float], None] = time.sleep,
                 recovery_attempts: int = 30,
                 recovery_clock: Callable[[], float] = time.monotonic,
                 recovery_budget_s: float = 30.0):
        self.cfg = cfg
        self.inbound = inbound_filter
        self.bridge = bridge
        self.sender = sender
        self.self_wxid = self_wxid
        self._on_pending = on_pending or (lambda m, r: None)
        self._on_auto = on_auto or (lambda m, r: None)
        self._is_ai_enabled = is_ai_enabled or (lambda: True)
        self._recovery_sleep = recovery_sleep
        self._recovery_attempts = max(1, int(recovery_attempts))
        self._recovery_clock = recovery_clock
        self._recovery_budget_s = max(1.0, float(recovery_budget_s))
        self._conv: dict[str, int] = {}        # contact_id -> conversation_id（会话粘连，多轮不分裂）

    def release_contact(self, contact_id: str) -> None:
        """保留给 M3 转人工浮窗调用。现在每条消息独立判断、不锁定联系人，故此处无需操作。"""
        return

    def _mark_delivery(self, result: dict, delivery_status: str) -> bool:
        message_id = result.get("outbound_message_id")
        if not message_id:
            return False
        attempt_id = str(result.get("delivery_attempt_id") or "")
        if delivery_status == "sending" and not attempt_id:
            attempt_id = uuid.uuid4().hex
            result["delivery_attempt_id"] = attempt_id
        for retry in range(2):
            try:
                response = self.bridge.mark_delivery(
                    int(message_id), delivery_status, attempt_id=attempt_id,
                )
                actual = str(response.get("delivery_status") or "")
                result["delivery_status"] = actual
                response_attempt = str(response.get("attempt_id") or "")
                result["recovered_expired_lease"] = bool(
                    response.get("recovered_expired_lease")
                )
                return bool(response.get("changed")) or (
                    actual == delivery_status
                    and bool(attempt_id)
                    and response_attempt == attempt_id
                )
            except (BridgeError, TypeError, ValueError):
                if retry == 0:
                    continue
                # 抖音私信投递结果是真相；回执 API 暂时失败不能改变已发生的发送，也不能中断待人工兜底。
                result["delivery_tracking_error"] = True
                return False

    def _request_answer(self, msg: InboundMsg, conversation_id: int | None) -> dict:
        last_error: BridgeError | None = None
        deadline: float | None = None
        for attempt in range(self._recovery_attempts):
            try:
                if attempt == 0:
                    result = self.bridge.chat(msg, conversation_id=conversation_id)
                else:
                    remaining = max(0.1, (deadline or 0.0) - self._recovery_clock())
                    result = self.bridge.chat(
                        msg, conversation_id=conversation_id,
                        timeout=min(5.0, remaining),
                    )
                if result.get("action") != "processing":
                    return result
            except BridgeError as exc:
                last_error = exc
            if deadline is None:
                deadline = self._recovery_clock() + self._recovery_budget_s
            if attempt + 1 < self._recovery_attempts and self._recovery_clock() < deadline:
                self._recovery_sleep(1.0)
            else:
                break
        raise last_error or BridgeError("reply is still processing after recovery timeout")

    def _was_already_delivered(
        self, contact_id: str, text: str, since_ts: int,
    ) -> bool:
        checker = getattr(self.sender, "was_delivered_since", None)
        return bool(checker and checker(contact_id, text, since_ts))

    def _reconcile_delivery(self, contact_id: str, text: str, since_ts: int) -> str:
        checker = getattr(self.sender, "reconcile_delivery", None)
        if checker is not None:
            return str(checker(contact_id, text, since_ts))
        return "delivered" if self._was_already_delivered(contact_id, text, since_ts) else "not_delivered"

    def _delivery_waiting(self, msg: InboundMsg, result: dict) -> str:
        result["pending_kind"] = "delivery_waiting"
        result["reconciliation_update"] = True
        self._on_pending(msg, result)
        return "delivery_waiting"

    def _finish_recovered_delivery(self, msg: InboundMsg, result: dict) -> str:
        action = str(result.get("action") or "")
        if action == "handoff":
            result["record_action"] = "handoff_notified"
            self._mark_delivery(result, "delivered")
            self._on_pending(msg, result)
            return "handoff"
        if action == "duplicate":
            result["reconciliation_only"] = True
        else:
            self._mark_delivery(result, "delivered")
        self._on_auto(msg, result)
        return "auto_reply"

    def _resume_delivery(self, msg: InboundMsg) -> str:
        """Finish a prior unknown side effect before applying current intake policy."""
        contact = msg["contact_id"]
        try:
            result = self._request_answer(msg, self._conv.get(contact))
        except BridgeError:
            return self._delivery_waiting(msg, {
                "action": "handoff", "reply_text": "", "delivery_status": "sending",
            })
        conversation_id = result.get("conversation_id")
        if conversation_id is not None:
            self._conv[contact] = conversation_id
        if result.get("action") == "duplicate" and result.get("delivery_status") == "delivered":
            return self._finish_recovered_delivery(msg, result)
        reply_text = str(result.get("reply_text") or "")
        if not result.get("outbound_message_id") or not reply_text:
            return self._delivery_waiting(msg, result)
        if not self._mark_delivery(result, "sending"):
            if result.get("delivery_status") == "delivered":
                return self._finish_recovered_delivery(msg, result)
            if result.get("delivery_status") == "sending":
                return self._delivery_waiting(msg, result)
            self._on_pending(msg, result)
            return "handoff"
        if result.get("recovered_expired_lease"):
            reconciliation = self._reconcile_delivery(
                contact, reply_text, int(msg.get("timestamp") or 0),
            )
            if reconciliation == "delivered":
                return self._finish_recovered_delivery(msg, result)
            if reconciliation != "not_delivered":
                return self._delivery_waiting(msg, result)

        action = str(result.get("action") or "handoff")
        scope_allowed = should_handle(msg, self.cfg.scope)
        ai_allowed = bool(self._is_ai_enabled())
        send_allowed = bool(self.cfg.auto_send and scope_allowed and ai_allowed)
        if action == "handoff":
            send_allowed = bool(send_allowed and self.cfg.handoff_reply)
        if not send_allowed:
            self._mark_delivery(result, "failed")
            if action == "auto_reply" and not self.cfg.auto_send:
                result["pending_kind"] = "auto_reply_draft"
                result["record_action"] = "auto_reply_draft"
                self._mark_delivery(result, "draft")
                self._on_pending(msg, result)
                return "auto_reply_draft"
            self._on_pending(msg, result)
            return "handoff"
        if self.sender.deliver(contact, reply_text):
            return self._finish_recovered_delivery(msg, result)
        if bool(getattr(getattr(self.sender, "last_result", None), "uncertain", False)):
            result["delivery_uncertain"] = True
            result["pending_kind"] = "delivery_uncertain"
            self._on_pending(msg, result)
            return "delivery_uncertain"
        self._mark_delivery(result, "failed")
        self._on_pending(msg, result)
        return "handoff"

    def handle(self, msg: InboundMsg) -> str:
        if msg.get("delivery_replay"):
            return self._resume_delivery(msg)
        if not self.inbound.accept(msg, self.self_wxid):
            return "ignored"
        if not should_handle(msg, self.cfg.scope):
            return "ignored"
        if not self._is_ai_enabled():
            self._on_pending(msg, {"action": "handoff", "reply_text": self.cfg.handoff_reply,
                                   "paused": True})
            return "handoff"
        contact = msg["contact_id"]
        try:
            result = self._request_answer(msg, self._conv.get(contact))
        except BridgeError:
            # 后端暂时不可用：转待人工但不锁定（恢复后 AI 继续）
            self._on_pending(msg, {"action": "handoff", "reply_text": self.cfg.handoff_reply,
                                   "error": True})
            return "error"
        cid = result.get("conversation_id")
        if cid is not None:
            self._conv[contact] = cid           # 记住会话，下一条带回，避免会话分裂
        if result.get("action") == "duplicate":
            return "ignored"
        if result.get("action") == "auto_reply":
            reply_text = result.get("reply_text")
            if reply_text and not self.cfg.auto_send:
                # 安全总闸关闭时，知识库已经回答成功，不应伪装成「AI 答不上」并触发待人工弹窗。
                # 留一条带 AI 草稿的静默待办，客服可人工确认发送；打开自动发送后走正常自动回复。
                result["pending_kind"] = "auto_reply_draft"
                result["record_action"] = "auto_reply_draft"
                self._mark_delivery(result, "draft")
                self._on_pending(msg, result)
                return "auto_reply_draft"
            if reply_text and self._mark_delivery(result, "sending"):
                if result.get("recovered_expired_lease"):
                    reconciliation = self._reconcile_delivery(
                        contact, reply_text, int(msg.get("timestamp") or 0),
                    )
                    if reconciliation == "delivered":
                        self._mark_delivery(result, "delivered")
                        self._on_auto(msg, result)
                        return "auto_reply"
                    if reconciliation != "not_delivered":
                        return self._delivery_waiting(msg, result)
                if self.sender.deliver(contact, reply_text):
                    self._mark_delivery(result, "delivered")
                    self._on_auto(msg, result)
                    return "auto_reply"
                if bool(getattr(getattr(self.sender, "last_result", None), "uncertain", False)):
                    # The browser may time out waiting for its first bubble snapshot even
                    # though Douyin has already accepted and rendered the message. Reconcile
                    # once immediately against verified history so a successfully delivered
                    # AI reply never appears in the human-handoff queue for five minutes.
                    reconciliation = self._reconcile_delivery(
                        contact, str(reply_text), int(msg.get("timestamp") or 0),
                    )
                    if reconciliation == "delivered" and self._mark_delivery(
                        result, "delivered",
                    ):
                        result["reconciled_immediately"] = True
                        self._on_auto(msg, result)
                        return "auto_reply"
                    result["delivery_uncertain"] = True
                    result["pending_kind"] = "delivery_uncertain"
                    self._on_pending(msg, result)
                    return "delivery_uncertain"
                else:
                    self._mark_delivery(result, "failed")
            elif reply_text:
                if result.get("delivery_status") == "sending":
                    return self._delivery_waiting(msg, result)
                return "ignored"
            # 该回但没发出去（关自动发/限速/发送失败）：转待人工，暂时性、不锁定
            self._on_pending(msg, result)
            return "handoff"
        # AI 判定答不了(handoff)：这条交人工，但不锁定——下一条能答的 AI 仍会自动回复
        if result.get("action") == "handoff":
            # 转人工提示也服从自动发送总闸；无论提示是否发出，原问题都继续留在待人工列表。
            result = dict(result)
            result["reply_text"] = self.cfg.handoff_reply
            delivery_attempted = False
            delivered = False
            if (self.cfg.auto_send and self.cfg.handoff_reply
                    and self._mark_delivery(result, "sending")):
                delivery_attempted = True
                if result.get("recovered_expired_lease"):
                    reconciliation = self._reconcile_delivery(
                        contact, self.cfg.handoff_reply,
                        int(msg.get("timestamp") or 0),
                    )
                    if reconciliation == "delivered":
                        delivered = True
                    elif reconciliation != "not_delivered":
                        return self._delivery_waiting(msg, result)
                if not delivered:
                    delivered = self.sender.deliver(contact, self.cfg.handoff_reply)
            elif (
                self.cfg.auto_send
                and self.cfg.handoff_reply
                and result.get("delivery_status") == "sending"
            ):
                return self._delivery_waiting(msg, result)
            if delivered:
                result["record_action"] = "handoff_notified"
                self._mark_delivery(result, "delivered")
            else:
                uncertain = bool(
                    getattr(getattr(self.sender, "last_result", None), "uncertain", False)
                )
                if uncertain and delivery_attempted:
                    result["delivery_uncertain"] = True
                    result["pending_kind"] = "delivery_uncertain"
                elif result.get("delivery_status") == "sending":
                    self._mark_delivery(result, "failed")
                elif not self.cfg.auto_send:
                    self._mark_delivery(result, "draft")
        self._on_pending(msg, result)
        if result.get("delivery_uncertain"):
            return "delivery_uncertain"
        return "handoff"
