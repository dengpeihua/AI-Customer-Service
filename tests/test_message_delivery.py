from __future__ import annotations

import datetime as dt
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.db import Base
from app.dialog.delivery import get_delivery_status, update_delivery_status
from app.models.conversation import Conversation, Message


class MessageDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        conversation = Conversation(
            tenant_id=1, channel="wechat_personal", contact_id="wxid_customer",
        )
        self.db.add(conversation)
        self.db.flush()
        self.message = Message(
            tenant_id=1,
            conversation_id=conversation.id,
            direction="out",
            sender="ai",
            provenance="ai",
            text="已经生成",
            meta={"delivery_status": "pending"},
        )
        self.db.add(self.message)
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_foreign_tenant_cannot_update_delivery(self) -> None:
        message, changed, recovered = update_delivery_status(
            self.db, tenant_id=2, message_id=self.message.id,
            delivery_status="delivered", attempt_id="attempt-1",
        )

        self.assertIsNone(message)
        self.assertFalse(changed)
        self.assertFalse(recovered)
        self.db.refresh(self.message)
        self.assertEqual("pending", get_delivery_status(self.message))

    def test_delivered_status_cannot_be_downgraded_by_late_failure(self) -> None:
        update_delivery_status(
            self.db, tenant_id=1, message_id=self.message.id,
            delivery_status="sending", attempt_id="attempt-1",
        )
        update_delivery_status(
            self.db, tenant_id=1, message_id=self.message.id,
            delivery_status="delivered", attempt_id="attempt-1",
        )
        result, changed, recovered = update_delivery_status(
            self.db, tenant_id=1, message_id=self.message.id,
            delivery_status="failed", attempt_id="attempt-1",
        )

        self.assertEqual("delivered", get_delivery_status(result))
        self.assertFalse(changed)
        self.assertFalse(recovered)

    def test_stale_session_failure_cannot_overwrite_delivered(self) -> None:
        stale = Session(self.engine)
        winner = Session(self.engine)
        try:
            stale.get(Message, self.message.id)  # 两边都先读到 pending
            update_delivery_status(
                winner, tenant_id=1, message_id=self.message.id,
                delivery_status="sending", attempt_id="attempt-1",
            )
            update_delivery_status(
                winner, tenant_id=1, message_id=self.message.id,
                delivery_status="delivered", attempt_id="attempt-1",
            )
            result, changed, recovered = update_delivery_status(
                stale, tenant_id=1, message_id=self.message.id,
                delivery_status="failed", attempt_id="attempt-1",
            )

            self.assertEqual("delivered", get_delivery_status(result))
            self.assertFalse(changed)
            self.assertFalse(recovered)
        finally:
            stale.close()
            winner.close()

    def test_same_attempt_can_retry_sending_ack_but_another_attempt_cannot_claim(self) -> None:
        first, changed, recovered = update_delivery_status(
            self.db, tenant_id=1, message_id=self.message.id,
            delivery_status="sending", attempt_id="attempt-1",
        )
        same, same_changed, same_recovered = update_delivery_status(
            self.db, tenant_id=1, message_id=self.message.id,
            delivery_status="sending", attempt_id="attempt-1",
        )
        other, other_changed, other_recovered = update_delivery_status(
            self.db, tenant_id=1, message_id=self.message.id,
            delivery_status="sending", attempt_id="attempt-2",
        )

        self.assertTrue(changed)
        self.assertTrue(same_changed)
        self.assertFalse(other_changed)
        self.assertEqual("attempt-1", other.delivery_attempt_id)
        self.assertFalse(recovered or same_recovered or other_recovered)

    def test_expired_sending_lease_can_be_claimed_by_new_attempt(self) -> None:
        claimed_at = dt.datetime(2026, 8, 12, tzinfo=dt.timezone.utc)
        update_delivery_status(
            self.db, tenant_id=1, message_id=self.message.id,
            delivery_status="sending", attempt_id="dead-process",
            now=claimed_at, lease_seconds=60,
        )

        result, changed, recovered = update_delivery_status(
            self.db, tenant_id=1, message_id=self.message.id,
            delivery_status="sending", attempt_id="new-process",
            now=claimed_at + dt.timedelta(seconds=61), lease_seconds=60,
        )

        self.assertTrue(changed)
        self.assertTrue(recovered)
        self.assertEqual("new-process", result.delivery_attempt_id)

        retried, retry_changed, retry_recovered = update_delivery_status(
            self.db, tenant_id=1, message_id=self.message.id,
            delivery_status="sending", attempt_id="new-process",
            now=claimed_at + dt.timedelta(seconds=62), lease_seconds=60,
        )
        self.assertTrue(retry_changed)
        self.assertTrue(retry_recovered)
        self.assertEqual("new-process", retried.delivery_recovered_attempt_id)


if __name__ == "__main__":
    unittest.main()
