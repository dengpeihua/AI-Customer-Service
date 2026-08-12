from __future__ import annotations

import os
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

import httpx

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QListWidgetItem, QMessageBox, QPushButton, QTabWidget, QTableWidgetItem,
)
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - register all tables
from app.crud.memory import create_memory
from app.db import Base
from app.memory.customer_service import CustomerMemoryService, memory_user_scope
from app.memory.mem0_gateway import Mem0Gateway, Mem0Unavailable
from app.models.customer import CustomerProfile
from app.models.memory import (
    CustomerMemory,
    MemoryIngestedMessage,
    MemoryOperation,
    MemorySyncState,
)
from app.routers.memories import MemoryCreate, MemoryDecisionDeleteIn, MemoryUpdate
from widget.config import WidgetConfig
from widget.bridge import Bridge
from widget.state import RuntimeState
from widget.ui.pages.memory_conversation_page import normalize_history_messages
from widget.ui.pages.operations_page import MemoryRecordsPage
from widget.ui.workbench import WorkbenchWindow


class _Mem0Gateway:
    def __init__(self) -> None:
        self.add_calls: list[dict] = []
        self.search_calls: list[dict] = []
        self.deleted: list[str] = []
        self.updated: list[tuple[str, str]] = []
        self.history_calls: list[tuple[str, str]] = []
        self.memories = [
            {"id": "pref-1", "memory": "[preference] 喜欢深蓝色", "created_at": "2026-08-09T10:00:00"},
            {"id": "need-1", "memory": "[need] 正在寻找本地部署方案", "created_at": "2026-08-09T10:01:00"},
        ]

    def add(self, messages, *, user_id, metadata=None, infer=True, custom_instructions=None,
            operation_id=None):
        self.add_calls.append({
            "messages": messages,
            "user_id": user_id,
            "metadata": metadata,
            "infer": infer,
            "custom_instructions": custom_instructions,
            "operation_id": operation_id,
        })
        return {"results": [dict(row, event="ADD") for row in self.memories]}

    def get_all(self, *, user_id, limit=200):
        return list(self.memories)

    def search(self, query, *, user_id, limit, filters=None):
        self.search_calls.append({"query": query, "user_id": user_id, "limit": limit, "filters": filters})
        return ([dict(self.memories[0], score=0.91, score_debug={"semantic": 0.88})], 6.5)

    def delete(self, memory_id, *, user_id, missing_ok=False):
        self.deleted.append(memory_id)

    def update(self, memory_id, content, *, user_id):
        self.updated.append((memory_id, content))

    def history(self, memory_id, *, user_id):
        self.history_calls.append((memory_id, user_id))
        return [{
            "event": "UPDATE", "old_memory": "喜欢红色", "new_memory": "喜欢深蓝色",
            "reason": "客户明确改变偏好",
        }]


class CustomerMemoryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, autoflush=False)
        self.gateway = _Mem0Gateway()
        self.service = CustomerMemoryService(self.gateway)

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_mem0_scope_is_tenant_and_contact_scoped_without_raw_contact_id(self) -> None:
        first = memory_user_scope(7, "wechat_personal", "wxid_private_person")
        second = memory_user_scope(7, "wechat_personal", "wxid_private_person")
        other_tenant = memory_user_scope(8, "wechat_personal", "wxid_private_person")

        self.assertEqual(first, second)
        self.assertNotEqual(first, other_tenant)
        self.assertNotIn("wxid_private_person", first)

    def test_greeting_noop_is_marked_processed_without_creating_memory(self) -> None:
        class NoopGateway(_Mem0Gateway):
            def add(self, *args, **kwargs):
                self.add_calls.append({"messages": args[0], **kwargs})
                return {"results": [{
                    "id": None, "memory": "", "event": "NOOP",
                    "reason": "no_durable_memory",
                }]}

            def get_all(self, *, user_id, limit=200):
                return []

        service = CustomerMemoryService(NoopGateway())
        result = service.ingest_conversation(
            self.db, tenant_id=1, channel="wechat_personal", contact_id="friend-noop",
            messages=[{"key": "greeting-1", "role": "user", "content": "谢谢，收到", "timestamp": 1}],
        )
        self.assertEqual("completed", result["status"])
        self.assertEqual(1, result["processed_messages"])
        self.assertEqual(0, result["memory_count"])
        self.assertEqual(2, self.db.query(MemoryIngestedMessage).count())
        self.assertEqual(
            ["NOOP"],
            [row["event"] for row in service.list_semantic_decisions(self.db, tenant_id=1)],
        )

    def test_memory_history_resolves_remote_id_with_contact_scope(self) -> None:
        self.service.ingest_conversation(
            self.db, tenant_id=1, channel="wechat_personal", contact_id="friend-history",
            messages=[{"key": "m1", "role": "user", "content": "我喜欢深蓝色", "timestamp": 1}],
        )
        memory = self.db.query(CustomerMemory).filter(
            CustomerMemory.contact_id == "friend-history"
        ).first()
        rows = self.service.memory_history(self.db, tenant_id=1, memory_id=memory.id)
        self.assertEqual("UPDATE", rows[0]["event"])
        self.assertEqual("pref-1", self.gateway.history_calls[0][0])
        self.assertEqual(
            memory_user_scope(1, "wechat_personal", "friend-history"),
            self.gateway.history_calls[0][1],
        )
        self.assertIsNone(self.service.memory_history(self.db, tenant_id=2, memory_id=memory.id))

    def test_semantic_delete_removes_only_scoped_local_mirror_and_keeps_audit(self) -> None:
        target = CustomerMemory(
            tenant_id=1, channel="wechat_personal", contact_id="friend-delete",
            memory_type="fact", content="旧事实", source="mem0", source_key="mem0:gone-1",
        )
        other = CustomerMemory(
            tenant_id=1, channel="wechat_personal", contact_id="other-friend",
            memory_type="fact", content="其他联系人事实", source="mem0", source_key="mem0:gone-1",
        )
        self.db.add_all([target, other]); self.db.commit()
        decisions = [{
            "id": "gone-1", "event": "DELETE", "old_memory": "旧事实",
            "memory": "旧事实", "reason": "客户明确否定",
        }]
        self.service._record_semantic_decisions(
            self.db, tenant_id=1, channel="wechat_personal", contact_id="friend-delete",
            decision_batch_id="delete-test", results=decisions,
        )
        self.service._apply_remote_deletes(
            self.db, tenant_id=1, channel="wechat_personal", contact_id="friend-delete",
            decisions=decisions,
        )
        self.db.commit()
        self.assertIsNone(self.db.get(CustomerMemory, target.id))
        self.assertIsNotNone(self.db.get(CustomerMemory, other.id))
        audit = self.service.list_semantic_decisions(self.db, tenant_id=1)
        self.assertEqual("DELETE", audit[0]["event"])

    def test_governance_delete_releases_source_chat_and_reingest_recreates_memory(self) -> None:
        class IdempotentGateway(_Mem0Gateway):
            def __init__(self) -> None:
                super().__init__()
                self.memories = []
                self.cached_operations: dict[str, dict] = {}
                self.operation_ids: list[str] = []
                self.next_id = 0

            def add(self, messages, *, user_id, metadata=None, infer=True,
                    custom_instructions=None, operation_id=None):
                self.operation_ids.append(str(operation_id or ""))
                if operation_id in self.cached_operations:
                    return self.cached_operations[operation_id]
                self.next_id += 1
                memory = {
                    "id": f"recreated-{self.next_id}",
                    "memory": "[preference] 喜欢深蓝色",
                    "metadata": dict(metadata or {}),
                }
                self.memories = [memory]
                result = {"results": [dict(memory, event="ADD")]}
                self.cached_operations[str(operation_id or "")] = result
                return result

            def delete(self, memory_id, *, user_id, missing_ok=False):
                super().delete(memory_id, user_id=user_id, missing_ok=missing_ok)
                self.memories = [row for row in self.memories if row["id"] != memory_id]

            def search(self, query, *, user_id, limit, filters=None):
                rows = [dict(row, score=0.95) for row in self.memories[:limit]]
                return rows, 1.0

        gateway = IdempotentGateway()
        service = CustomerMemoryService(gateway)
        messages = [{
            "key": "selected-chat-1", "role": "user",
            "content": "我一直喜欢深蓝色", "timestamp": 1,
        }]

        first = service.ingest_conversation(
            self.db, tenant_id=1, channel="wechat_personal",
            contact_id="friend-reingest", messages=messages,
        )
        first_memory_id = first["memories"][0]["id"]
        self.assertTrue(service.delete_memory(self.db, tenant_id=1, memory_id=first_memory_id))

        self.assertEqual(0, self.db.query(CustomerMemory).count())
        self.assertEqual(0, self.db.query(MemoryIngestedMessage).count())
        deleted_state = self.db.query(MemorySyncState).one()
        self.assertEqual("needs_reingest", deleted_state.last_status)
        self.assertEqual(0, deleted_state.messages_processed)

        recreated = service.ingest_conversation(
            self.db, tenant_id=1, channel="wechat_personal",
            contact_id="friend-reingest", messages=messages,
        )
        recalled = service.recall(
            tenant_id=1, channel="wechat_personal", contact_id="friend-reingest",
            query="喜欢什么颜色", limit=5,
        )
        governed = self.db.query(CustomerMemory).filter(
            CustomerMemory.tenant_id == 1,
            CustomerMemory.channel == "wechat_personal",
            CustomerMemory.contact_id == "friend-reingest",
        ).all()

        self.assertEqual(1, recreated["processed_messages"])
        self.assertEqual(1, recreated["memory_count"])
        self.assertEqual("completed", self.db.query(MemorySyncState).one().last_status)
        self.assertEqual("喜欢深蓝色", recreated["memories"][0]["content"])
        self.assertEqual("喜欢深蓝色", recalled["results"][0]["memory"])
        self.assertEqual(["喜欢深蓝色"], [row.content for row in governed])
        self.assertNotEqual(gateway.operation_ids[0], gateway.operation_ids[1])
        self.assertEqual(
            ["ADD", "DELETE", "ADD"],
            list(reversed([
                row["event"]
                for row in service.list_semantic_decisions(self.db, tenant_id=1)
            ])),
        )

    def test_manual_governance_crud_records_add_update_delete_semantics(self) -> None:
        memory = self.service.create_manual_memory(
            self.db, tenant_id=1, channel="wechat_personal", contact_id="friend-manual",
            memory_type="preference", content="喜欢蓝色",
        )
        self.service.update_memory(
            self.db, tenant_id=1, memory_id=memory.id, content="喜欢深蓝色",
        )
        self.assertTrue(self.service.delete_memory(self.db, tenant_id=1, memory_id=memory.id))

        self.assertEqual(
            ["DELETE", "UPDATE", "ADD"],
            [
                row["event"]
                for row in self.service.list_semantic_decisions(
                    self.db, tenant_id=1, contact_id="friend-manual"
                )
            ],
        )

    def test_legacy_completed_delete_is_repaired_when_same_chat_is_selected_again(self) -> None:
        messages = [{
            "key": "legacy-deleted-source", "role": "user",
            "content": "我偏好简洁回复", "timestamp": 1,
        }]
        first = self.service.ingest_conversation(
            self.db, tenant_id=1, channel="wechat_personal",
            contact_id="friend-legacy-delete", messages=messages,
        )
        for memory in self.db.query(CustomerMemory).all():
            self.db.delete(memory)
        self.gateway.memories = []
        self.db.add(MemoryOperation(
            tenant_id=1, channel="wechat_personal", contact_id="friend-legacy-delete",
            operation_id="legacy-delete-operation", kind="delete",
            payload={"memory_id": first["memories"][0]["id"], "remote_id": "pref-1"},
            status="completed", attempts=1,
        ))
        self.db.commit()
        self.gateway.memories = [{
            "id": "pref-recreated", "memory": "[preference] 偏好简洁回复",
            "created_at": "2026-08-10T10:00:00",
        }]

        recreated = self.service.ingest_conversation(
            self.db, tenant_id=1, channel="wechat_personal",
            contact_id="friend-legacy-delete", messages=messages,
        )

        self.assertEqual(1, recreated["processed_messages"])
        self.assertEqual(1, recreated["memory_count"])
        legacy = self.db.scalar(select(MemoryOperation).where(
            MemoryOperation.operation_id == "legacy-delete-operation"
        ))
        self.assertTrue(legacy.payload.get("ledger_released"))

    def test_governance_delete_releases_only_the_deleted_memory_source_batch(self) -> None:
        class BatchGateway(_Mem0Gateway):
            def __init__(self) -> None:
                super().__init__()
                self.memories = []

            def add(self, messages, *, user_id, metadata=None, infer=True,
                    custom_instructions=None, operation_id=None):
                content = messages[0]["content"]
                remote_id = "color-memory" if "深蓝色" in content else "deploy-memory"
                text = (
                    "[preference] 喜欢深蓝色"
                    if remote_id == "color-memory"
                    else "[need] 正在寻找本地部署方案"
                )
                row = {"id": remote_id, "memory": text, "metadata": dict(metadata or {})}
                self.memories = [item for item in self.memories if item["id"] != remote_id]
                self.memories.append(row)
                return {"results": [dict(row, event="ADD")]}

            def delete(self, memory_id, *, user_id, missing_ok=False):
                self.memories = [row for row in self.memories if row["id"] != memory_id]

        service = CustomerMemoryService(BatchGateway())
        first_message = [{
            "key": "source-color", "role": "user",
            "content": "我一直喜欢深蓝色", "timestamp": 1,
        }]
        second_message = [{
            "key": "source-deploy", "role": "user",
            "content": "我正在寻找本地部署方案", "timestamp": 2,
        }]
        service.ingest_conversation(
            self.db, tenant_id=1, channel="wechat_personal",
            contact_id="friend-two-batches", messages=first_message,
        )
        service.ingest_conversation(
            self.db, tenant_id=1, channel="wechat_personal",
            contact_id="friend-two-batches", messages=second_message,
        )
        color = self.db.scalar(select(CustomerMemory).where(
            CustomerMemory.source_key == "mem0:color-memory"
        ))

        self.assertTrue(service.delete_memory(self.db, tenant_id=1, memory_id=color.id))

        remaining_keys = self.db.scalars(select(MemoryIngestedMessage.message_key)).all()
        self.assertEqual(2, len(remaining_keys))
        retried = service.ingest_conversation(
            self.db, tenant_id=1, channel="wechat_personal",
            contact_id="friend-two-batches", messages=first_message + second_message,
        )
        self.assertEqual(1, retried["processed_messages"])

    def test_deleting_manual_memory_does_not_release_conversation_dedupe_ledger(self) -> None:
        self.service.ingest_conversation(
            self.db, tenant_id=1, channel="wechat_personal", contact_id="friend-manual-delete",
            messages=[{
                "key": "conversation-source", "role": "user",
                "content": "我喜欢深蓝色", "timestamp": 1,
            }],
        )
        manual = CustomerMemory(
            tenant_id=1, channel="wechat_personal", contact_id="friend-manual-delete",
            memory_type="note", content="人工备注", source="manual", source_key=None,
        )
        self.db.add(manual)
        self.db.commit()

        self.assertTrue(self.service.delete_memory(self.db, tenant_id=1, memory_id=manual.id))

        self.assertEqual(2, self.db.query(MemoryIngestedMessage).count())

    def test_update_source_batches_are_released_with_the_governed_memory(self) -> None:
        class UpdatingGateway(_Mem0Gateway):
            def __init__(self) -> None:
                super().__init__()
                self.memories = []
                self.calls = 0

            def add(self, messages, *, user_id, metadata=None, infer=True,
                    custom_instructions=None, operation_id=None):
                self.calls += 1
                old = self.memories[0]["memory"] if self.memories else None
                text = (
                    "[preference] 喜欢深蓝色"
                    if self.calls == 1
                    else "[preference] 喜欢深蓝色且偏好简洁回复"
                )
                self.memories = [{"id": "updated-memory", "memory": text}]
                return {"results": [{
                    "id": "updated-memory", "memory": text,
                    "old_memory": old,
                    "event": "ADD" if self.calls == 1 else "UPDATE",
                }]}

            def delete(self, memory_id, *, user_id, missing_ok=False):
                self.memories = []

        service = CustomerMemoryService(UpdatingGateway())
        service.ingest_conversation(
            self.db, tenant_id=1, channel="wechat_personal", contact_id="friend-update",
            messages=[{
                "key": "add-source", "role": "user",
                "content": "我喜欢深蓝色", "timestamp": 1,
            }],
        )
        service.ingest_conversation(
            self.db, tenant_id=1, channel="wechat_personal", contact_id="friend-update",
            messages=[{
                "key": "update-source", "role": "user",
                "content": "我还偏好简洁回复", "timestamp": 2,
            }],
        )
        memory = self.db.scalar(select(CustomerMemory).where(
            CustomerMemory.source_key == "mem0:updated-memory"
        ))

        self.assertTrue(service.delete_memory(self.db, tenant_id=1, memory_id=memory.id))

        self.assertEqual(0, self.db.query(MemoryIngestedMessage).count())
        self.assertEqual(
            ["DELETE", "UPDATE", "ADD"],
            [
                row["event"]
                for row in service.list_semantic_decisions(
                    self.db, tenant_id=1, contact_id="friend-update"
                )
            ],
        )

    def test_delete_semantic_decisions_is_batch_scoped_to_the_current_tenant(self) -> None:
        rows = [
            MemoryOperation(
                tenant_id=1, channel="wechat_personal", contact_id="friend-a",
                operation_id="decision-a", kind="semantic_decision",
                payload={"event": "ADD"}, status="completed",
            ),
            MemoryOperation(
                tenant_id=1, channel="wechat_personal", contact_id="friend-a",
                operation_id="decision-b", kind="semantic_decision",
                payload={"event": "UPDATE"}, status="completed",
            ),
            MemoryOperation(
                tenant_id=2, channel="wechat_personal", contact_id="friend-b",
                operation_id="decision-other-tenant", kind="semantic_decision",
                payload={"event": "DELETE"}, status="completed",
            ),
            MemoryOperation(
                tenant_id=1, channel="wechat_personal", contact_id="friend-a",
                operation_id="non-decision", kind="memory_create",
                payload={}, status="completed",
            ),
            MemoryOperation(
                tenant_id=1, channel="wechat_personal", contact_id="friend-a",
                operation_id="pending-decision", kind="semantic_decision",
                payload={"event": "ADD"}, status="pending",
            ),
        ]
        self.db.add_all(rows)
        self.db.commit()

        deleted = self.service.delete_semantic_decisions(
            self.db,
            tenant_id=1,
            decision_ids=[
                rows[0].id, rows[1].id, rows[2].id, rows[3].id, rows[4].id, rows[0].id,
            ],
        )

        self.assertEqual(2, deleted)
        self.assertIsNone(self.db.get(MemoryOperation, rows[0].id))
        self.assertIsNone(self.db.get(MemoryOperation, rows[1].id))
        self.assertIsNotNone(self.db.get(MemoryOperation, rows[2].id))
        self.assertIsNotNone(self.db.get(MemoryOperation, rows[3].id))
        self.assertIsNotNone(self.db.get(MemoryOperation, rows[4].id))

    def test_ingest_real_chat_uses_mem0_and_rebuilds_long_term_profile(self) -> None:
        messages = [
            {"key": "m1", "role": "user", "content": "我喜欢深蓝色", "timestamp": 100},
            {"key": "m2", "role": "assistant", "content": "记住了", "timestamp": 101},
            {"key": "m3", "role": "user", "content": "我正在找本地部署方案", "timestamp": 102},
        ]

        first = self.service.ingest_conversation(
            self.db, tenant_id=1, channel="wechat_personal", contact_id="wxid_a",
            messages=messages, display_name="小林",
        )
        second = self.service.ingest_conversation(
            self.db, tenant_id=1, channel="wechat_personal", contact_id="wxid_a",
            messages=messages, display_name="小林",
        )

        self.assertEqual(3, first["processed_messages"])
        self.assertEqual(0, second["processed_messages"])
        self.assertEqual(1, len(self.gateway.add_calls))
        self.assertEqual(["user", "assistant", "user"], [m["role"] for m in self.gateway.add_calls[0]["messages"]])
        self.assertEqual(2, first["memory_count"])
        profile = self.db.query(CustomerProfile).one()
        self.assertIn("喜欢深蓝色", profile.summary)
        self.assertIn("本地部署方案", profile.summary)
        self.assertIn("偏好", profile.tags)
        self.assertIn("需求", profile.tags)
        self.assertTrue(self.gateway.add_calls[0]["custom_instructions"])
        state = self.db.query(MemorySyncState).one()
        self.assertTrue(state.last_message_key.startswith("customer-profile-v3-classic-mem0:"))
        self.assertNotIn("m3", state.last_message_key)

    def test_live_exchange_then_manual_selection_skips_the_same_content(self) -> None:
        self.service.remember_exchange(
            self.db,
            tenant_id=1,
            channel="wechat_personal",
            contact_id="wxid_live_manual",
            customer_text="我有一只小狗叫麻将，它是一只很乖的小狗",
            reply_text="听起来麻将真的很可爱",
            exchange_id="chat-message:101",
        )

        result = self.service.ingest_conversation(
            self.db,
            tenant_id=1,
            channel="wechat_personal",
            contact_id="wxid_live_manual",
            messages=[
                {
                    "key": "local-wechat-9001",
                    "role": "user",
                    "content": "我有一只小狗叫麻将，它是一只很乖的小狗",
                    "timestamp": 100,
                },
                {
                    "key": "local-wechat-9002",
                    "role": "assistant",
                    "content": "听起来麻将真的很可爱",
                    "timestamp": 101,
                },
            ],
        )

        self.assertEqual("up_to_date", result["status"])
        self.assertEqual(0, result["processed_messages"])
        self.assertEqual(2, result["skipped_messages"])
        self.assertEqual(1, len(self.gateway.add_calls))
        self.assertTrue(self.gateway.add_calls[0]["operation_id"].startswith("live:"))
        decisions = self.service.list_semantic_decisions(
            self.db, tenant_id=1, contact_id="wxid_live_manual",
        )
        self.assertTrue(decisions)
        self.assertEqual(2, len(decisions[0]["source_message_keys"]))

        # Retrying the same live exchange is also local-idempotent and must not call Mem0 again.
        self.service.remember_exchange(
            self.db,
            tenant_id=1,
            channel="wechat_personal",
            contact_id="wxid_live_manual",
            customer_text="我有一只小狗叫麻将，它是一只很乖的小狗",
            reply_text="听起来麻将真的很可爱",
            exchange_id="chat-message:101",
        )
        self.assertEqual(1, len(self.gateway.add_calls))

    def test_manual_selection_after_live_ingest_processes_only_new_content(self) -> None:
        self.service.remember_exchange(
            self.db,
            tenant_id=1,
            channel="wechat_personal",
            contact_id="wxid_mixed_manual",
            customer_text="我和我弟弟感情很好",
            exchange_id="chat-message:201",
        )

        result = self.service.ingest_conversation(
            self.db,
            tenant_id=1,
            channel="wechat_personal",
            contact_id="wxid_mixed_manual",
            messages=[
                {
                    "key": "local-wechat-9101",
                    "role": "user",
                    "content": "我和我弟弟感情很好",
                    "timestamp": 200,
                },
                {
                    "key": "local-wechat-9102",
                    "role": "user",
                    "content": "我最近开始学习摄影",
                    "timestamp": 201,
                },
            ],
        )

        self.assertEqual("completed", result["status"])
        self.assertEqual(1, result["processed_messages"])
        self.assertEqual(1, result["skipped_messages"])
        self.assertEqual(2, len(self.gateway.add_calls))
        self.assertEqual(
            ["我最近开始学习摄影"],
            [row["content"] for row in self.gateway.add_calls[1]["messages"]],
        )

    def test_live_exchange_empty_mem0_result_is_not_marked_processed(self) -> None:
        class EmptyAddGateway(_Mem0Gateway):
            def add(self, *args, **kwargs):
                self.add_calls.append({"messages": args[0], **kwargs})
                return {"results": []}

        service = CustomerMemoryService(EmptyAddGateway())

        with self.assertRaisesRegex(ValueError, "no semantic decisions"):
            service.remember_exchange(
                self.db,
                tenant_id=1,
                channel="wechat_personal",
                contact_id="wxid_live_empty",
                customer_text="我有一只小狗叫麻将",
                exchange_id="chat-message:empty",
            )

        self.assertEqual(0, self.db.query(MemoryIngestedMessage).count())

    def test_empty_mem0_extraction_does_not_consume_selected_messages(self) -> None:
        self.gateway.memories = []
        messages = [{
            "key": "durable-1",
            "role": "user",
            "content": "我长期偏好深蓝色，并且正在寻找本地部署方案",
            "timestamp": 100,
        }]

        with self.assertRaisesRegex(ValueError, "没有提炼出长期记忆"):
            self.service.ingest_conversation(
                self.db,
                tenant_id=1,
                channel="wechat_personal",
                contact_id="wxid_empty",
                messages=messages,
            )

        self.assertEqual(0, self.db.query(MemoryIngestedMessage).count())
        state = self.db.query(MemorySyncState).one()
        self.assertEqual(0, state.messages_processed)
        self.assertEqual("failed", state.last_status)
        self.assertEqual("", state.last_message_key)

    def test_empty_batch_is_not_consumed_when_contact_already_has_memories(self) -> None:
        class EmptyAddGateway(_Mem0Gateway):
            def add(self, *args, **kwargs):
                super().add(*args, **kwargs)
                return {"results": []}

        gateway = EmptyAddGateway()
        service = CustomerMemoryService(gateway)

        with self.assertRaisesRegex(ValueError, "没有提炼出长期记忆"):
            service.ingest_conversation(
                self.db,
                tenant_id=1,
                channel="wechat_personal",
                contact_id="wxid_existing_profile",
                messages=[{
                    "key": "new-fact-1",
                    "role": "user",
                    "content": "我现在需要完全本地部署",
                    "timestamp": 100,
                }],
            )

        self.assertEqual(0, self.db.query(MemoryIngestedMessage).count())

    def test_new_extractor_version_can_retry_messages_consumed_by_legacy_empty_run(self) -> None:
        self.db.add(MemoryIngestedMessage(
            tenant_id=1,
            channel="wechat_personal",
            contact_id="wxid_legacy_empty",
            message_key="legacy-message-1",
        ))
        self.db.add(MemorySyncState(
            tenant_id=1,
            channel="wechat_personal",
            contact_id="wxid_legacy_empty",
            messages_processed=100,
            last_status="completed",
        ))
        self.db.commit()

        result = self.service.ingest_conversation(
            self.db,
            tenant_id=1,
            channel="wechat_personal",
            contact_id="wxid_legacy_empty",
            messages=[{
                "key": "legacy-message-1",
                "role": "user",
                "content": "我长期偏好深蓝色",
                "timestamp": 100,
            }],
        )

        self.assertEqual(1, result["processed_messages"])
        self.assertEqual(1, len(self.gateway.add_calls))
        self.assertEqual(3, self.db.query(MemoryIngestedMessage).count())
        self.assertEqual(1, self.db.query(MemorySyncState).one().messages_processed)

    def test_unselected_older_message_can_be_ingested_later(self) -> None:
        first_selection = [
            {"key": "m1", "role": "user", "content": "第一条已选", "timestamp": 100},
            {"key": "m3", "role": "user", "content": "第三条已选", "timestamp": 102},
        ]
        later_selection = [
            {"key": "m2", "role": "user", "content": "第二条后来才选", "timestamp": 101},
        ]

        first = self.service.ingest_conversation(
            self.db, tenant_id=1, channel="wechat_personal", contact_id="wxid_selective",
            messages=first_selection,
        )
        second = self.service.ingest_conversation(
            self.db, tenant_id=1, channel="wechat_personal", contact_id="wxid_selective",
            messages=later_selection,
        )

        self.assertEqual(2, first["processed_messages"])
        self.assertEqual(1, second["processed_messages"])
        self.assertEqual(
            [["第一条已选", "第三条已选"], ["第二条后来才选"]],
            [[row["content"] for row in call["messages"]] for call in self.gateway.add_calls],
        )

    def test_identity_is_canonicalized_before_database_and_mem0_use(self) -> None:
        result = self.service.ingest_conversation(
            self.db, tenant_id=1, channel=" wechat_personal ", contact_id=" wxid_trim ",
            messages=[{"key": "m1", "role": "user", "content": "稳定事实", "timestamp": 1}],
        )

        self.assertEqual("wechat_personal", result["channel"])
        self.assertEqual("wxid_trim", result["contact_id"])

    def test_recall_searches_only_the_selected_real_contact(self) -> None:
        result = self.service.recall(
            tenant_id=3, channel="wechat_personal", contact_id="wxid_selected",
            query="他喜欢什么颜色？", limit=6,
        )

        self.assertEqual(1, result["result_count"])
        self.assertEqual("喜欢深蓝色", result["results"][0]["memory"])
        self.assertNotIn("wxid_selected", self.gateway.search_calls[0]["user_id"])
        self.assertEqual(6, self.gateway.search_calls[0]["limit"])

    def test_large_chat_history_is_batched_before_calling_mem0(self) -> None:
        messages = [
            {"key": f"m{index}", "role": "user", "content": f"长期信息 {index}", "timestamp": index}
            for index in range(45)
        ]

        result = self.service.ingest_conversation(
            self.db, tenant_id=1, channel="wechat_personal", contact_id="wxid_large",
            messages=messages,
        )

        self.assertEqual(45, result["processed_messages"])
        self.assertEqual([40, 5], [len(call["messages"]) for call in self.gateway.add_calls])

    def test_same_contact_concurrent_ingest_is_serialized(self) -> None:
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(engine)
        gateway = _Mem0Gateway()
        service = CustomerMemoryService(gateway)
        messages = [{"key": "only", "role": "user", "content": "稳定事实", "timestamp": 1}]

        def ingest():
            with Session(engine, autoflush=False) as db:
                return service.ingest_conversation(
                    db, tenant_id=9, channel="wechat_personal", contact_id="wxid_lock",
                    messages=messages,
                )["processed_messages"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            processed = sorted(pool.map(lambda _index: ingest(), range(2)))
        engine.dispose()

        self.assertEqual([0, 1], processed)
        self.assertEqual(1, len(gateway.add_calls))

    def test_batch_checkpoint_resumes_after_last_committed_message(self) -> None:
        class FailingGateway(_Mem0Gateway):
            def __init__(self):
                super().__init__()
                self.fail_on_call = 2

            def add(self, *args, **kwargs):
                if len(self.add_calls) + 1 == self.fail_on_call:
                    self.add_calls.append({"failed": True})
                    raise RuntimeError("batch failed")
                return super().add(*args, **kwargs)

        gateway = FailingGateway()
        service = CustomerMemoryService(gateway)
        messages = [
            {"key": f"m{i}", "role": "user", "content": f"事实 {i}", "timestamp": i}
            for i in range(45)
        ]
        with self.assertRaises(RuntimeError):
            service.ingest_conversation(
                self.db, tenant_id=4, channel="wechat_personal",
                contact_id="wxid_resume", messages=messages,
            )
        gateway.fail_on_call = -1

        resumed = service.ingest_conversation(
            self.db, tenant_id=4, channel="wechat_personal",
            contact_id="wxid_resume", messages=messages,
        )

        self.assertEqual(5, resumed["processed_messages"])
        successful_sizes = [len(call["messages"]) for call in gateway.add_calls if "messages" in call]
        self.assertEqual([40, 5], successful_sizes)

    def test_manual_create_recovers_from_persistent_outbox_after_commit_failure(self) -> None:
        original_commit = self.db.commit
        attempts = 0

        def fail_final_commit():
            nonlocal attempts
            attempts += 1
            if attempts == 3:
                raise RuntimeError("commit failed")
            return original_commit()

        self.db.commit = fail_final_commit
        try:
            with self.assertRaises(RuntimeError):
                self.service.create_manual_memory(
                    self.db, tenant_id=1, channel="wechat_personal", contact_id="wxid_saga",
                    memory_type="note", content="recoverable note",
                )
        finally:
            self.db.commit = original_commit

        operation = self.db.query(MemoryOperation).one()
        self.assertEqual("failed", operation.status)
        profile = self.service.list_profile(
            self.db, tenant_id=1, channel="wechat_personal", contact_id="wxid_saga"
        )
        self.assertEqual("completed", self.db.get(MemoryOperation, operation.id).status)
        self.assertIn("recoverable note", profile["summary"])

    def test_update_recovers_from_persistent_outbox_after_commit_failure(self) -> None:
        memory = CustomerMemory(
            tenant_id=1, channel="wechat_personal", contact_id="wxid_saga",
            memory_type="fact", content="old content", source="mem0", source_key="mem0:remote-1",
            importance=0.5, is_pinned=False,
        )
        self.db.add(memory)
        self.db.commit()
        original_commit = self.db.commit
        attempts = 0

        def fail_final_commit():
            nonlocal attempts
            attempts += 1
            if attempts == 3:
                raise RuntimeError("commit failed")
            return original_commit()

        self.db.commit = fail_final_commit
        try:
            with self.assertRaises(RuntimeError):
                self.service.update_memory(
                    self.db, tenant_id=1, memory_id=memory.id, content="new content"
                )
        finally:
            self.db.commit = original_commit

        self.service.list_profile(
            self.db, tenant_id=1, channel="wechat_personal", contact_id="wxid_saga"
        )
        self.assertEqual("new content", self.db.get(CustomerMemory, memory.id).content)
        self.assertEqual(2, len(self.gateway.updated))

    def test_delete_recovers_from_persistent_outbox_after_commit_failure(self) -> None:
        memory = CustomerMemory(
            tenant_id=1, channel="wechat_personal", contact_id="wxid_delete_saga",
            memory_type="note", content="delete safely", source="mem0", source_key="mem0:remote-delete",
            importance=0.5, is_pinned=False,
        )
        self.db.add(memory)
        self.db.commit()
        memory_id = memory.id
        original_commit = self.db.commit
        attempts = 0

        def fail_final_commit():
            nonlocal attempts
            attempts += 1
            if attempts == 3:
                raise RuntimeError("transient commit failed")
            return original_commit()

        self.db.commit = fail_final_commit
        try:
            with self.assertRaises(RuntimeError):
                self.service.delete_memory(self.db, tenant_id=1, memory_id=memory_id)
        finally:
            self.db.commit = original_commit

        self.assertIsNotNone(self.db.get(CustomerMemory, memory_id))
        self.service.list_profile(
            self.db, tenant_id=1, channel="wechat_personal", contact_id="wxid_delete_saga"
        )
        self.assertIsNone(self.db.get(CustomerMemory, memory_id))
        self.assertIn("remote-delete", self.gateway.deleted)

    def test_delete_mem0_memory_removes_remote_before_local_record(self) -> None:
        self.service.ingest_conversation(
            self.db, tenant_id=1, channel="wechat_personal", contact_id="wxid_a",
            messages=[{"key": "m1", "role": "user", "content": "喜欢深蓝色", "timestamp": 100}],
        )
        profile_memories = self.service.list_profile(
            self.db, tenant_id=1, channel="wechat_personal", contact_id="wxid_a"
        )["memories"]
        preference = next(row for row in profile_memories if "深蓝色" in row["content"])
        memory_id = first_id = preference["id"]

        removed = self.service.delete_memory(self.db, tenant_id=1, memory_id=memory_id)

        self.assertTrue(removed)
        self.assertIn("pref-1", self.gateway.deleted)
        remaining_ids = [row["id"] for row in self.service.list_profile(
            self.db, tenant_id=1, channel="wechat_personal", contact_id="wxid_a"
        )["memories"]]
        self.assertNotIn(first_id, remaining_ids)

    def test_existing_manual_memory_without_source_key_remains_in_long_term_profile(self) -> None:
        create_memory(
            self.db, 1, channel="wechat_personal", contact_id="wxid_legacy",
            memory_type="note", content="迁移前人工备注", source="manual",
            source_key=None, importance=0.6, is_pinned=False,
        )

        profile = self.service.list_profile(
            self.db, tenant_id=1, channel="wechat_personal", contact_id="wxid_legacy"
        )

        self.assertEqual(["迁移前人工备注"], [row["content"] for row in profile["memories"]])

    def test_manual_memory_updates_profile_with_production_autoflush_disabled(self) -> None:
        created = self.service.create_manual_memory(
            self.db, tenant_id=1, channel="wechat_personal", contact_id="wxid_manual",
            memory_type="preference", content="偏好简洁回复", importance=0.8,
        )

        profile = self.service.list_profile(
            self.db, tenant_id=1, channel="wechat_personal", contact_id="wxid_manual"
        )
        self.assertEqual(created.id, profile["memories"][0]["id"])
        self.assertIn("偏好简洁回复", profile["summary"])
        self.assertFalse(self.gateway.add_calls[0]["infer"])

    def test_pin_update_is_flushed_before_top_twelve_profile_rebuild(self) -> None:
        for index in range(13):
            self.db.add(CustomerMemory(
                tenant_id=1, channel="wechat_personal", contact_id="wxid_rank",
                memory_type="fact", content=f"事实 {index}", source="manual",
                importance=0.9 if index < 12 else 0.1, is_pinned=False,
            ))
        self.db.commit()
        target = self.db.query(CustomerMemory).filter_by(content="事实 12").one()

        self.service.update_memory(
            self.db, tenant_id=1, memory_id=target.id, is_pinned=True
        )

        profile = self.db.query(CustomerProfile).filter_by(contact_id="wxid_rank").one()
        self.assertIn("事实 12", profile.summary)


class MemoryRequestValidationTests(unittest.TestCase):
    def test_whitespace_is_stripped_and_empty_values_are_rejected(self) -> None:
        body = MemoryCreate(channel=" wechat_personal ", contact_id=" wxid_a ", content=" note ")
        self.assertEqual("wechat_personal", body.channel)
        self.assertEqual("wxid_a", body.contact_id)
        with self.assertRaises(ValidationError):
            MemoryCreate(channel="   ", contact_id="wxid_a", content="note")

    def test_patch_rejects_explicit_null_before_remote_update(self) -> None:
        with self.assertRaises(ValidationError):
            MemoryUpdate(content=None)

    def test_decision_delete_rejects_empty_or_invalid_ids_and_deduplicates(self) -> None:
        self.assertEqual([7, 9], MemoryDecisionDeleteIn(ids=[7, 7, 9]).ids)
        with self.assertRaises(ValidationError):
            MemoryDecisionDeleteIn(ids=[])
        with self.assertRaises(ValidationError):
            MemoryDecisionDeleteIn(ids=[0])

    def test_bridge_uses_the_tenant_authenticated_batch_delete_endpoint(self) -> None:
        class Response:
            status_code = 200
            def raise_for_status(self): pass
            def json(self): return {"deleted": 2}

        class Client:
            def __init__(self): self.request_args = None
            def request(self, method, path, **kwargs):
                self.request_args = (method, path, kwargs)
                return Response()

        client = Client()
        bridge = Bridge(WidgetConfig(), client=client)
        bridge.token = "test-token"

        result = bridge.delete_memory_decisions([7, 9])

        self.assertEqual({"deleted": 2}, result)
        self.assertEqual(("DELETE", "/v1/memories/decisions"), client.request_args[:2])
        self.assertEqual({"ids": [7, 9]}, client.request_args[2]["json"])


class MemoryConversationNormalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_real_wechat_history_is_normalized_for_mem0_without_media_noise(self) -> None:
        rows = normalize_history_messages([
            {"local_id": 1, "ts": 100, "is_self": False, "text": "我喜欢蓝色", "type": "text"},
            {"local_id": 2, "ts": 101, "is_self": True, "text": "好的", "type": "text"},
            {"local_id": 3, "ts": 102, "is_self": False, "text": "", "type": "image"},
        ])

        self.assertEqual(2, len(rows))
        self.assertEqual(["user", "assistant"], [row["role"] for row in rows])
        self.assertEqual(["1", "2"], [row["key"] for row in rows])

    def test_returning_to_page_keeps_both_panels_during_transient_empty_refresh(self) -> None:
        class Adapter:
            def list_sessions(self):
                return []
            def display_names(self, _ids):
                return {}

        page = __import__(
            "widget.ui.pages.memory_conversation_page", fromlist=["MemoryConversationPage"]
        ).MemoryConversationPage(_WorkbenchBridge(), adapter=Adapter())
        queued = []

        def fake_start(callback, completed, failed):
            queued.append((callback, completed, failed))
            return True

        try:
            page._sessions = [{
                "contact_id": "wxid_saved", "display_name": "已显示好友", "is_group": False,
            }]
            page._session_list.blockSignals(True)
            page._session_list.addItem("已显示好友")
            page._session_list.setCurrentRow(0)
            page._session_list.blockSignals(False)
            page._loaded_identity = (page._channel_key, "wxid_saved")
            page._apply_messages([{
                "local_id": 1, "ts": 1, "is_self": False, "text": "已经显示的聊天",
            }])
            page._start = fake_start

            page.activate()

            self.assertEqual(1, page._session_list.count())
            self.assertEqual(1, page._table.rowCount())
            self.assertEqual("已经显示的聊天", page._table.item(0, 3).text())
            self.assertEqual(1, len(queued))

            queued[0][1](([], {}))

            self.assertEqual(1, page._session_list.count())
            self.assertEqual(1, page._table.rowCount())
            self.assertEqual("已经显示的聊天", page._table.item(0, 3).text())
        finally:
            page.close(); page.deleteLater()

    def test_explicit_refresh_keeps_visible_memory_conversation_snapshot(self) -> None:
        class Adapter:
            def list_sessions(self):
                return []

        page = __import__(
            "widget.ui.pages.memory_conversation_page", fromlist=["MemoryConversationPage"]
        ).MemoryConversationPage(_WorkbenchBridge(), adapter=Adapter())
        queued = []
        try:
            page._sessions = [{
                "contact_id": "wxid_saved", "display_name": "已显示好友", "is_group": False,
            }]
            page._session_list.addItem("已显示好友")
            page._session_list.setCurrentRow(0)
            page._loaded_identity = (page._channel_key, "wxid_saved")
            page._apply_messages([{
                "local_id": 1, "ts": 1, "is_self": False, "text": "不要在刷新时清空我",
            }])
            page._start = lambda callback, completed, failed: (
                queued.append((callback, completed, failed)) or True
            )

            page.refresh()

            self.assertEqual(1, page._session_list.count())
            self.assertEqual("不要在刷新时清空我", page._table.item(0, 3).text())
            self.assertTrue(
                page._requested_sessions_preserve or page._sessions_refresh_pending_preserve
            )
        finally:
            page.close(); page.deleteLater()

    def test_session_list_is_published_without_waiting_for_display_name_query(self) -> None:
        class Adapter:
            def list_sessions(self):
                return [{"wxid": "wxid_fast", "summary": "新消息"}]
            def display_names(self, _ids):
                raise AssertionError("联系人名不应阻塞会话列表首屏")

        page = __import__(
            "widget.ui.pages.memory_conversation_page", fromlist=["MemoryConversationPage"]
        ).MemoryConversationPage(_WorkbenchBridge(), adapter=Adapter())
        captured = []
        try:
            page._start = lambda callback, completed, failed: (
                captured.append(callback()) or True
            )

            page.refresh()

            sessions, names = captured[0]
            self.assertEqual("wxid_fast", sessions[0]["wxid"])
            self.assertEqual({}, names)
        finally:
            page.close(); page.deleteLater()

    def test_slow_database_snapshot_cannot_hide_a_live_message(self) -> None:
        class Adapter:
            def read_conversation(self, _contact, _limit):
                return []

        page = __import__(
            "widget.ui.pages.memory_conversation_page", fromlist=["MemoryConversationPage"]
        ).MemoryConversationPage(_WorkbenchBridge(), adapter=Adapter())
        try:
            page._start = lambda *_args: False
            channel = page._channel_key
            identity = (channel, "wxid_live", 1)
            page._requested_identity = identity
            page.apply_live_event({
                "event_id": "in:m-live",
                "direction": "inbound",
                "channel": channel,
                "contact_id": "wxid_live",
                "sender_id": "wxid_live",
                "text": "数据库还没落盘的新消息",
                "timestamp": 10,
            })
            page._requested_identity = identity

            page._messages_ready(identity, [])

            self.assertEqual(1, page._table.rowCount())
            self.assertEqual("数据库还没落盘的新消息", page._table.item(0, 3).text())
        finally:
            page.close(); page.deleteLater()

    def test_history_snapshot_populates_memory_page_before_background_query(self) -> None:
        page = __import__(
            "widget.ui.pages.memory_conversation_page", fromlist=["MemoryConversationPage"]
        ).MemoryConversationPage(_WorkbenchBridge(), adapter=None)
        try:
            page.seed_snapshot({
                "channel": page._channel_key,
                "sessions": [{"wxid": "wxid_seed", "summary": "已有摘要", "ts": 5}],
                "names": {"wxid_seed": "已有好友"},
                "contact_id": "wxid_seed",
                "messages": [{
                    "local_id": 5, "ts": 5, "is_self": False, "text": "会话中心已经加载",
                }],
            })

            self.assertEqual("已有好友", page._sessions[0]["display_name"])
            self.assertEqual(1, page._table.rowCount())
            self.assertEqual("会话中心已经加载", page._table.item(0, 3).text())
        finally:
            page.close(); page.deleteLater()

    def test_background_activation_keeps_the_selected_contact_and_messages(self) -> None:
        class Adapter:
            def list_sessions(self):
                return [
                    {"wxid": "wxid_a", "summary": "A 新摘要"},
                    {"wxid": "wxid_b", "summary": "B 新摘要"},
                ]
            def display_names(self, ids):
                return {value: value[-1].upper() for value in ids}

        page = __import__(
            "widget.ui.pages.memory_conversation_page", fromlist=["MemoryConversationPage"]
        ).MemoryConversationPage(_WorkbenchBridge(), adapter=Adapter())
        queued = []
        try:
            page._sessions = [
                {"contact_id": "wxid_a", "display_name": "A", "is_group": False},
                {"contact_id": "wxid_b", "display_name": "B", "is_group": False},
            ]
            page._session_list.blockSignals(True)
            page._session_list.addItems(["A", "B"])
            page._session_list.setCurrentRow(1)
            page._session_list.blockSignals(False)
            page._loaded_identity = (page._channel_key, "wxid_b")
            page._apply_messages([{
                "local_id": 2, "ts": 2, "is_self": False, "text": "B 已显示消息",
            }])
            page._start = lambda callback, completed, failed: (
                queued.append((callback, completed, failed)) or True
            )

            page.activate()
            queued[0][1](queued[0][0]())

            self.assertEqual(1, page._session_list.currentRow())
            self.assertEqual((page._channel_key, "wxid_b"), page._loaded_identity)
            self.assertEqual("B 已显示消息", page._table.item(0, 3).text())
        finally:
            page.close(); page.deleteLater()

    def test_read_limit_accepts_two_and_is_passed_to_the_selected_conversation(self) -> None:
        class Adapter:
            def __init__(self): self.calls = []
            def read_conversation(self, contact_id, limit):
                self.calls.append((contact_id, limit))
                return [
                    {"local_id": 1, "ts": 1, "is_self": False, "text": "第一条"},
                    {"local_id": 2, "ts": 2, "is_self": True, "text": "第二条"},
                ]

        adapter = Adapter()
        page = __import__(
            "widget.ui.pages.memory_conversation_page", fromlist=["MemoryConversationPage"]
        ).MemoryConversationPage(_WorkbenchBridge(), adapter=adapter)
        try:
            page._sessions = [{"contact_id": "wxid_limit", "display_name": "两条测试"}]
            page._session_list.blockSignals(True)
            page._session_list.addItem("两条测试")
            page._session_list.setCurrentRow(0)
            page._session_list.blockSignals(False)
            page._start = lambda callback, completed, _failed: (completed(callback()), True)[1]

            page._limit.setValue(2)

            self.assertEqual(1, page._limit.minimum())
            self.assertEqual([("wxid_limit", 2)], adapter.calls)
            self.assertEqual(2, page._table.rowCount())
        finally:
            page.close(); page.deleteLater()

    def test_only_checked_messages_are_confirmed_and_uploaded(self) -> None:
        events = []

        class RecordingBridge:
            def __init__(self): self.messages = []
            def ingest_memory_conversation(self, _channel, _contact, messages, **_kwargs):
                events.append("request")
                self.messages = messages
                return {"processed_messages": len(messages), "memory_count": 1}

        bridge = RecordingBridge()
        page = __import__(
            "widget.ui.pages.memory_conversation_page", fromlist=["MemoryConversationPage"]
        ).MemoryConversationPage(bridge, adapter=None)
        try:
            page._sessions = [{"contact_id": "wxid_pick", "display_name": "选择测试"}]
            page._session_list.addItem("选择测试")
            page._session_list.setCurrentRow(0)
            page._loaded_identity = (page._channel_key, "wxid_pick")
            page._apply_messages([
                {"local_id": 1, "ts": 1, "is_self": False, "text": "不要上传"},
                {"local_id": 2, "ts": 2, "is_self": False, "text": "只上传我"},
                {"local_id": 3, "ts": 3, "is_self": True, "text": "也不要上传"},
            ])
            self.assertEqual(4, page._table.columnCount())
            self.assertEqual(Qt.CheckState.Unchecked, page._table.item(0, 0).checkState())
            page._table.item(1, 0).setCheckState(Qt.CheckState.Checked)
            page._confirm_ingest = lambda count, _name: events.append(("confirm", count)) or True
            page._start = lambda callback, completed, _failed: (completed(callback()), True)[1]

            page._ingest_selected()

            self.assertEqual([("confirm", 1), "request"], events)
            self.assertEqual(["只上传我"], [row["content"] for row in bridge.messages])
        finally:
            page.close(); page.deleteLater()

    def test_select_all_button_selects_every_loaded_text_message(self) -> None:
        page = __import__(
            "widget.ui.pages.memory_conversation_page", fromlist=["MemoryConversationPage"]
        ).MemoryConversationPage(_WorkbenchBridge(), adapter=None)
        try:
            page._apply_messages([
                {"local_id": 1, "ts": 1, "is_self": False, "text": "一"},
                {"local_id": 2, "ts": 2, "is_self": True, "text": "二"},
            ])
            page._select_all_messages()
            self.assertEqual(
                [Qt.CheckState.Checked, Qt.CheckState.Checked],
                [page._table.item(row, 0).checkState() for row in range(2)],
            )
        finally:
            page.close(); page.deleteLater()

    def test_memory_ingest_uses_a_long_request_timeout(self) -> None:
        class Response:
            status_code = 200
            def raise_for_status(self): pass
            def json(self): return {"processed_messages": 1, "memory_count": 1}

        class Client:
            def __init__(self): self.kwargs = None
            def request(self, *_args, **kwargs):
                self.kwargs = kwargs
                return Response()

        client = Client()
        bridge = Bridge(WidgetConfig(), client=client)
        bridge.token = "test-token"
        bridge.ingest_memory_conversation(
            "wechat_personal", "wxid_timeout",
            [{"key": "1", "role": "user", "content": "记住我", "timestamp": 1}],
        )

        self.assertGreaterEqual(float(client.kwargs["timeout"]), 120.0)

    def test_memory_ingest_reports_real_batch_progress(self) -> None:
        class Response:
            status_code = 200
            def __init__(self, size: int): self.size = size
            def raise_for_status(self): pass
            def json(self):
                return {
                    "processed_messages": max(0, self.size - 1),
                    "skipped_messages": 1,
                    "memory_count": 3,
                }

        class Client:
            def __init__(self): self.batch_sizes = []
            def request(self, *_args, **kwargs):
                size = len(kwargs["json"]["messages"])
                self.batch_sizes.append(size)
                return Response(size)

        client = Client()
        bridge = Bridge(WidgetConfig(), client=client)
        bridge.token = "test-token"
        progress = []
        result = bridge.ingest_memory_conversation(
            "wechat_personal", "wxid_progress",
            [{"key": str(i), "role": "user", "content": f"消息 {i}", "timestamp": i}
             for i in range(85)],
            on_progress=lambda done, total, stage: progress.append((done, total, stage)),
        )

        self.assertEqual([40, 40, 5], client.batch_sizes)
        self.assertEqual((0, 85), progress[0][:2])
        self.assertEqual((85, 85), progress[-1][:2])
        self.assertIn("第 3/3 批完成", progress[-1][2])
        self.assertEqual(82, result["processed_messages"])
        self.assertEqual(3, result["skipped_messages"])
        self.assertEqual(3, result["batch_count"])

    def test_memory_page_explains_reply_flow_and_shows_progress(self) -> None:
        page = __import__(
            "widget.ui.pages.memory_conversation_page", fromlist=["MemoryConversationPage"]
        ).MemoryConversationPage(_WorkbenchBridge(), adapter=None)
        try:
            guide = page._memory_usage.text()
            self.assertIn("真实微信对话", guide)
            self.assertIn("语义召回", guide)
            self.assertIn("客服回复", guide)
            self.assertIn("非业务闲聊或情绪支持", guide)
            self.assertIn("涉及产品、售后、投诉等使用知识库", guide)
            for memory_type in ("事实", "偏好", "需求", "承诺", "备注"):
                self.assertIn(memory_type, guide)

            page._begin_ingest_progress(85)
            page._on_ingest_progress(40, 85, "第 1/3 批完成，准备下一批")

            self.assertFalse(page._progress_bar.isHidden())
            self.assertEqual(85, page._progress_bar.maximum())
            self.assertEqual(40, page._progress_bar.value())
            self.assertIn("40 / 85", page._progress_bar.format())
            self.assertTrue(page._progress_timer.isActive())

            page._ingest_done({
                "processed_messages": 80,
                "skipped_messages": 5,
                "memory_count": 3,
            })
            self.assertFalse(page._progress_timer.isActive())
            self.assertEqual(85, page._progress_bar.value())
            self.assertIn("完成", page._progress_bar.format())
            self.assertIn("已去重 5 条", page._status.text())
        finally:
            page.close(); page.deleteLater()

    def test_memory_progress_stops_at_last_completed_batch_on_failure(self) -> None:
        page = __import__(
            "widget.ui.pages.memory_conversation_page", fromlist=["MemoryConversationPage"]
        ).MemoryConversationPage(_WorkbenchBridge(), adapter=None)
        try:
            page._begin_ingest_progress(85)
            page._on_ingest_progress(40, 85, "第 1/3 批完成，正在处理第 2 批")
            page._ingest_failed("模型服务超时")

            self.assertFalse(page._progress_timer.isActive())
            self.assertEqual(40, page._progress_bar.value())
            self.assertIn("中止", page._progress_bar.format())
            self.assertIn("记忆提取失败", page._status.text())
            self.assertIn("安全重试", page._progress_elapsed.text())
        finally:
            page.close(); page.deleteLater()

    def test_recall_page_previews_stored_memories_before_a_query(self) -> None:
        class BridgeWithProfile:
            def get_long_term_profile(self, channel, contact_id):
                self.request = (channel, contact_id)
                return {
                    "contact_id": contact_id,
                    "memories": [{
                        "memory_type": "preference", "content": "喜欢深蓝色",
                        "source": "mem0", "updated_at": "2026-08-10T10:00:00",
                    }],
                }

        bridge = BridgeWithProfile()
        page = __import__(
            "widget.ui.pages.memory_recall_page", fromlist=["MemoryRecallPage"]
        ).MemoryRecallPage(bridge)
        try:
            page._start = lambda callback, completed, _failed: (completed(callback()), True)[1]
            page._apply_contacts([{
                "channel": "wechat_personal", "contact_id": "wxid_preview",
                "display_name": "预览用户", "_bridge_key": "wechat_personal",
            }])

            self.assertEqual(("wechat_personal", "wxid_preview"), bridge.request)
            self.assertEqual(1, page._table.rowCount())
            self.assertEqual("喜欢深蓝色", page._table.item(0, 1).text())
            self.assertIn("已存长期记忆", page._status.text())
            self.assertIn("不是独立聊天机器人", page._reply_context.text())
            self.assertIn("客服主链路", page._reply_context.text())
        finally:
            page.close(); page.deleteLater()

    def test_recall_page_explicitly_says_it_retrieves_without_generating_a_reply(self) -> None:
        page = __import__(
            "widget.ui.pages.memory_recall_page", fromlist=["MemoryRecallPage"]
        ).MemoryRecallPage(_WorkbenchBridge())
        try:
            button_texts = [button.text() for button in page.findChildren(QPushButton)]
            self.assertIn("检索相关记忆（不生成回复）", button_texts)
            self.assertIn("不会生成客服回复", page._reply_context.text())
            self.assertIn("客户当前消息", page._query.placeholderText())
        finally:
            page.close(); page.deleteLater()

    def test_long_term_page_explains_an_empty_processed_profile(self) -> None:
        page = __import__(
            "widget.ui.pages.long_term_memory_page", fromlist=["LongTermMemoryPage"]
        ).LongTermMemoryPage(_WorkbenchBridge())
        try:
            page._apply_profile({
                "contact_id": "wxid_empty_profile",
                "memories": [],
                "sync": {"status": "completed", "messages_processed": 100},
            })

            self.assertEqual(1, page._table.rowCount())
            self.assertIn("重新", page._table.item(0, 0).text())
            self.assertIn("未形成长期记忆", page._sync.text())
        finally:
            page.close(); page.deleteLater()

    def test_recall_preview_retries_the_latest_contact_after_a_fast_switch(self) -> None:
        class BridgeWithProfiles:
            def get_long_term_profile(self, _channel, contact_id):
                return {"memories": [{"memory_type": "fact", "content": f"{contact_id} 的记忆"}]}

        page = __import__(
            "widget.ui.pages.memory_recall_page", fromlist=["MemoryRecallPage"]
        ).MemoryRecallPage(BridgeWithProfiles())
        queued = []
        busy = False

        def fake_start(callback, completed, failed):
            nonlocal busy
            if busy:
                return False
            busy = True
            queued.append((callback, completed, failed))
            return True

        def complete(index):
            nonlocal busy
            callback, completed, _failed = queued[index]
            busy = False
            completed(callback())

        try:
            page._start = fake_start
            page._apply_contacts([
                {"channel": "wechat_personal", "contact_id": "wxid_a", "display_name": "A"},
                {"channel": "wechat_personal", "contact_id": "wxid_b", "display_name": "B"},
            ])
            page._contacts_box.setCurrentIndex(1)

            complete(0)
            self.assertEqual(2, len(queued))
            complete(1)

            self.assertEqual("wxid_b 的记忆", page._table.item(0, 1).text())
        finally:
            page.close(); page.deleteLater()

    def test_channel_switch_discards_old_sessions_before_retrying_current_channel(self) -> None:
        class Adapter:
            def __init__(self, key):
                self.key = key
                self.read_calls = []
            def list_sessions(self):
                return [{"wxid": f"{self.key}_contact", "name": self.key}]
            def display_names(self, _ids): return {}
            def read_conversation(self, contact_id, limit):
                self.read_calls.append((contact_id, limit))
                return [{"local_id": 1, "ts": 1, "is_self": False, "text": contact_id}]

        adapters = {"channel-a": Adapter("a"), "channel-b": Adapter("b")}
        page = __import__(
            "widget.ui.pages.memory_conversation_page", fromlist=["MemoryConversationPage"]
        ).MemoryConversationPage(
            _WorkbenchBridge(), channels=[("channel-a", "A"), ("channel-b", "B")],
            adapter_for=lambda key: adapters[key], default_channel="channel-a",
        )
        queued = []
        busy = False

        def fake_start(callback, completed, failed):
            nonlocal busy
            if busy:
                return False
            busy = True
            queued.append((callback, completed, failed))
            return True

        def complete(index):
            nonlocal busy
            callback, completed, _failed = queued[index]
            busy = False
            completed(callback())

        try:
            page._start = fake_start
            page._sessions = [{"contact_id": "a_contact", "display_name": "A", "is_group": False}]
            page._session_list.addItem("A")
            page._session_list.setCurrentRow(0)
            self.assertEqual(1, len(queued))

            page._channel.setCurrentIndex(1)
            self.assertEqual([], page._sessions)
            self.assertEqual(0, page._session_list.count())

            complete(0)
            complete(1)
            complete(2)

            self.assertFalse(adapters["channel-b"].read_calls == [("a_contact", 100)])
            self.assertEqual([("b_contact", 100)], adapters["channel-b"].read_calls)
            self.assertEqual("b_contact", page._messages[0]["text"])
        finally:
            page.close(); page.deleteLater()

    def test_ingest_uses_the_bridge_bound_to_the_selected_channel(self) -> None:
        class RecordingBridge:
            def __init__(self): self.calls = []
            def ingest_memory_conversation(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return {"processed_messages": 1, "memory_count": 1}

        bridge_a, bridge_b = RecordingBridge(), RecordingBridge()
        page = __import__(
            "widget.ui.pages.memory_conversation_page", fromlist=["MemoryConversationPage"]
        ).MemoryConversationPage(
            bridge_a, channels=[("channel-a", "A"), ("channel-b", "B")],
            bridge_for=lambda key: {"channel-a": bridge_a, "channel-b": bridge_b}[key],
        )
        try:
            page._channel.blockSignals(True)
            page._channel.setCurrentIndex(1)
            page._channel.blockSignals(False)
            page._channel_key = "channel-b"
            page._sessions = [{"contact_id": "wxid_b", "display_name": "B好友"}]
            page._session_list.addItem(QListWidgetItem("B好友"))
            page._session_list.setCurrentRow(0)
            page._loaded_identity = ("channel-b", "wxid_b")
            page._apply_messages([
                {"local_id": 1, "ts": 1, "is_self": False, "text": "B 的聊天"}
            ])
            page._table.item(0, 0).setCheckState(Qt.CheckState.Checked)
            page._confirm_ingest = lambda _count, _name: True
            page._start = lambda callback, completed, _failed: (completed(callback()), True)[1]

            page._ingest_selected()

            self.assertFalse(bridge_a.calls)
            self.assertEqual("channel-b", bridge_b.calls[0][0][0])
            self.assertEqual("wxid_b", bridge_b.calls[0][0][1])
        finally:
            page.close(); page.deleteLater()

    def test_stale_async_result_cannot_be_uploaded_for_newly_selected_friend(self) -> None:
        class Adapter:
            def read_conversation(self, contact_id, _limit): return [{"text": contact_id}]

        page = __import__(
            "widget.ui.pages.memory_conversation_page", fromlist=["MemoryConversationPage"]
        ).MemoryConversationPage(_WorkbenchBridge(), adapter=Adapter())
        queued = []
        starts = 0

        def fake_start(callback, completed, failed):
            nonlocal starts
            starts += 1
            if starts == 2:
                return False
            queued.append((callback, completed, failed))
            return True

        try:
            page._sessions = [
                {"contact_id": "wxid_a", "display_name": "A"},
                {"contact_id": "wxid_b", "display_name": "B"},
            ]
            page._session_list.blockSignals(True)
            page._session_list.addItems(["A", "B"])
            page._session_list.setCurrentRow(0)
            page._session_list.blockSignals(False)
            page._start = fake_start
            page._load_selected(0)
            page._session_list.blockSignals(True)
            page._session_list.setCurrentRow(1)
            page._session_list.blockSignals(False)
            page._load_selected(1)

            queued[0][1]([{"local_id": 1, "ts": 1, "is_self": False, "text": "A 私聊"}])
            self.assertGreaterEqual(len(queued), 2)
            queued[1][1]([{"local_id": 2, "ts": 2, "is_self": False, "text": "B 私聊"}])

            self.assertEqual((page._channel_key, "wxid_b"), page._loaded_identity)
            self.assertEqual("B 私聊", page._messages[0]["text"])
        finally:
            page.close(); page.deleteLater()

    def test_group_sessions_use_a_separate_scope_and_keep_the_sender_name(self) -> None:
        page = __import__(
            "widget.ui.pages.memory_conversation_page", fromlist=["MemoryConversationPage"]
        ).MemoryConversationPage(_WorkbenchBridge(), adapter=None)
        try:
            page._apply_sessions(([
                {"wxid": "room@chatroom", "name": "群聊"},
                {"wxid": "wxid_friend", "name": "好友"},
            ], {}))
            self.assertEqual(
                ["room@chatroom", "wxid_friend"],
                [row["contact_id"] for row in page._sessions],
            )
            self.assertTrue(page._sessions[0]["is_group"])
            normalized = normalize_history_messages([{
                "local_id": 9, "ts": 9, "is_self": False, "is_group": True,
                "sender_id": "wxid_member", "sender_name": "张三", "text": "喜欢蓝色",
            }])
            self.assertEqual("张三：喜欢蓝色", normalized[0]["content"])
        finally:
            page.close(); page.deleteLater()


class MemoryGovernanceDeleteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_delete_is_submitted_while_governance_history_is_loading(self) -> None:
        class Bridge:
            def __init__(self) -> None:
                self.history_started = threading.Event()
                self.release_history = threading.Event()
                self.deleted = threading.Event()

            def memory_history(self, _memory_id: int) -> dict:
                self.history_started.set()
                self.release_history.wait(2)
                return {"events": []}

            def delete_memory(self, _memory_id: int) -> None:
                self.deleted.set()

            def list_memories(self, **_kwargs):
                return []

            def memory_stats(self):
                return {"total": 0, "pinned": 0, "by_type": {}}

            def list_memory_decisions(self, **_kwargs):
                return {"events": []}

        bridge = Bridge()
        page = MemoryRecordsPage(bridge, governance=True)
        row = {
            "id": 42,
            "channel": "wechat_personal",
            "contact_id": "synthetic-contact",
            "memory_type": "preference",
            "content": "synthetic preference",
            "source": "mem0",
            "importance": 0.5,
            "is_pinned": False,
        }
        try:
            page._apply({
                "rows": [row],
                "decisions": [],
                "stats": {"total": 1, "pinned": 0, "by_type": {}},
            })
            page._table.setCurrentCell(0, 0)
            page._load_selected_history(row)
            self.assertTrue(bridge.history_started.wait(1))
            page._table.item(0, 0).setCheckState(Qt.CheckState.Checked)

            with patch(
                "widget.ui.pages.operations_page.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                page._memory_delete_selected.click()

            self.assertTrue(
                bridge.deleted.wait(1),
                "delete must not wait for the read-only history request",
            )
        finally:
            bridge.release_history.set()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and (
                page._task is not None or page._history_task is not None
            ):
                self.app.processEvents()
                time.sleep(0.01)
            page.close()
            page.deleteLater()

    def test_memory_records_support_selective_and_select_all_deletion(self) -> None:
        class Bridge:
            def __init__(self) -> None:
                self.deleted_memory_ids: list[int] = []

            def delete_memory(self, memory_id: int) -> None:
                self.deleted_memory_ids.append(memory_id)

            def list_memories(self, **_kwargs):
                return []

            def memory_stats(self):
                return {"total": 0, "pinned": 0, "by_type": {}}

            def list_memory_decisions(self, **_kwargs):
                return {"events": []}

        bridge = Bridge()
        page = MemoryRecordsPage(bridge, governance=True)
        rows = [
            {
                "id": 21, "channel": "wechat_personal", "contact_id": "friend-a",
                "memory_type": "preference", "content": "喜欢蓝色", "source": "mem0",
                "importance": 0.5, "is_pinned": False,
            },
            {
                "id": 22, "channel": "wechat_personal", "contact_id": "friend-b",
                "memory_type": "fact", "content": "养了一只小狗", "source": "mem0",
                "importance": 0.6, "is_pinned": False,
            },
        ]
        payload = {
            "rows": rows,
            "decisions": [],
            "stats": {"total": 2, "pinned": 0, "by_type": {}},
        }
        try:
            page._apply(payload)
            page._memory_select_all.click()
            page._memory_clear_all.click()
            self.assertTrue(all(
                page._table.item(row, 0).checkState() == Qt.CheckState.Unchecked
                for row in range(page._table.rowCount())
            ))
            self.assertFalse(page._memory_delete_selected.isEnabled())

            page._table.item(0, 0).setCheckState(Qt.CheckState.Checked)
            page._table.item(1, 0).setCheckState(Qt.CheckState.Unchecked)
            page._start = lambda callback, completed, _failed: (completed(callback()), True)[1]
            page.refresh = Mock()

            with patch(
                "widget.ui.pages.operations_page.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                page._memory_delete_selected.click()

            self.assertEqual([21], bridge.deleted_memory_ids)

            bridge.deleted_memory_ids.clear()
            page._apply(payload)
            page._memory_select_all.click()
            self.assertTrue(all(
                page._table.item(row, 0).checkState() == Qt.CheckState.Checked
                for row in range(page._table.rowCount())
            ))
            with patch(
                "widget.ui.pages.operations_page.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                page._memory_delete_selected.click()

            self.assertEqual([21, 22], bridge.deleted_memory_ids)
        finally:
            page.close()
            page.deleteLater()

    def test_semantic_decisions_support_selective_and_select_all_deletion(self) -> None:
        class Bridge:
            def __init__(self) -> None:
                self.deleted_decision_ids: list[list[int]] = []

            def delete_memory_decisions(self, decision_ids: list[int]) -> dict:
                self.deleted_decision_ids.append(list(decision_ids))
                return {"deleted": len(decision_ids)}

            def list_memories(self, **_kwargs):
                return []

            def memory_stats(self):
                return {"total": 0, "pinned": 0, "by_type": {}}

            def list_memory_decisions(self, **_kwargs):
                return {"events": []}

        bridge = Bridge()
        page = MemoryRecordsPage(bridge, governance=True)
        decisions = [
            {"id": 11, "channel": "wechat_personal", "contact_id": "friend-a", "event": "ADD"},
            {"id": 12, "channel": "wechat_personal", "contact_id": "friend-a", "event": "UPDATE"},
        ]
        try:
            page._apply_decisions(decisions)
            page._history_table.item(0, 0).setCheckState(Qt.CheckState.Checked)
            page._history_table.item(1, 0).setCheckState(Qt.CheckState.Unchecked)
            page._start = lambda callback, completed, _failed: (completed(callback()), True)[1]
            page.refresh = Mock()

            with patch(
                "widget.ui.pages.operations_page.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                page._decision_delete.click()

            self.assertEqual([[11]], bridge.deleted_decision_ids)

            bridge.deleted_decision_ids.clear()
            page._apply_decisions(decisions)
            page._decision_select_all.click()
            self.assertTrue(all(
                page._history_table.item(row, 0).checkState() == Qt.CheckState.Checked
                for row in range(page._history_table.rowCount())
            ))
            with patch(
                "widget.ui.pages.operations_page.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                page._decision_delete.click()

            self.assertEqual([[11, 12]], bridge.deleted_decision_ids)
        finally:
            page.close()
            page.deleteLater()


class Mem0GatewayContractTests(unittest.TestCase):
    def test_real_score_details_and_metadata_are_preserved(self) -> None:
        class Response:
            def raise_for_status(self): pass
            def json(self):
                return {"results": [{
                    "id": "m1", "memory": "[preference] 蓝色", "score": 0.9,
                    "score_details": {"semantic": 0.8},
                    "metadata": {"source": "manual"},
                }]}

        class Client:
            def __init__(self, **_kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *_args): pass
            def post(self, *_args, **_kwargs): return Response()

        with patch("app.memory.mem0_gateway.httpx.Client", Client):
            rows, _latency = Mem0Gateway("http://mem0", service_token="token").search(
                "颜色", user_id="scope", limit=3
            )

        self.assertEqual({"semantic": 0.8}, rows[0]["score_debug"])
        self.assertEqual("manual", rows[0]["source"])
        self.assertIsNone(rows[0]["memory_type"])

    def test_server_error_includes_bounded_companion_detail(self) -> None:
        request = httpx.Request("POST", "http://mem0/memories")
        response = httpx.Response(
            500,
            request=request,
            json={
                "detail": (
                    "LLM memory update returned an invalid short id; "
                    "Authorization: Bearer sk-secret-123 api_key=abc"
                )
            },
        )

        class Client:
            def __init__(self, **_kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *_args): pass
            def request(self, *_args, **_kwargs): return response

        with patch("app.memory.mem0_gateway.httpx.Client", Client):
            with self.assertRaises(Mem0Unavailable) as raised:
                Mem0Gateway("http://mem0", service_token="token").add(
                    [{"role": "user", "content": "durable fact"}],
                    user_id="scope",
                )
        detail = str(raised.exception)
        self.assertIn("HTTP 500", detail)
        self.assertIn("invalid short id", detail)
        self.assertNotIn("sk-secret-123", detail)
        self.assertNotIn("api_key=abc", detail)


class _WorkbenchBridge:
    def list_memories(self, **_kwargs):
        return []

    def memory_stats(self):
        return {"total": 0, "pinned": 0, "by_type": {}}


class _Controller:
    def pending(self):
        return []


class MemoryNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_memory_domain_has_four_product_pages_and_benchmark_under_functional_tests(self) -> None:
        window = WorkbenchWindow(
            RuntimeState(WidgetConfig()), _Controller(), _WorkbenchBridge(), adapter=None,
        )
        try:
            self.assertLess(window._idx["memory_conversation"], window._idx["long_term_memory"])
            for name in ("memory_conversation", "long_term_memory", "memory_recall", "memory_governance"):
                self.assertIn(name, window._idx)
            tabs = window.functional_tests_page.findChild(QTabWidget, "FunctionalTestTabs")
            self.assertIsNotNone(tabs)
            self.assertIn("LoCoMo 公开集评测", [tabs.tabText(i) for i in range(tabs.count())])
        finally:
            window.close()
            window.deleteLater()

    def test_navigation_back_to_memory_conversation_uses_non_destructive_activation(self) -> None:
        window = WorkbenchWindow(
            RuntimeState(WidgetConfig()), _Controller(), _WorkbenchBridge(), adapter=None,
        )
        try:
            window.memory_conversation_page.activate = Mock()
            window.memory_conversation_page.refresh = Mock(side_effect=AssertionError(
                "navigation must not clear the visible memory-conversation snapshot"
            ))

            window._switch(window._idx["memory_conversation"])

            window.memory_conversation_page.activate.assert_called_once_with()
            window.memory_conversation_page.refresh.assert_not_called()
        finally:
            window.close()
            window.deleteLater()


if __name__ == "__main__":
    unittest.main()
