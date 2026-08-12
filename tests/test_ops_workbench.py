from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest.mock import MagicMock, patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QPushButton
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.db import Base
from app.models.conversation import Conversation, Message
from app.models.deal import Deal
from app.models.knowledge import KbChunk, KbDocument
from app.models.memory import CustomerMemory
from app.models.tenant import Tenant
from app.ops.runtime import OpsTaskRegistry
from app.ops.service import browse_dataset, delete_dataset_rows, ops_overview
from widget.config import WidgetConfig
from widget.state import RuntimeState
from widget.ui.workbench import WorkbenchWindow
from widget.ui.pages.operations_page import DataBrowserPage, MemoryRecordsPage, WorkerTasksPage


class OpsServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.db.add_all([Tenant(id=1, name="A"), Tenant(id=2, name="B")])
        self.db.flush()
        a_conv = Conversation(tenant_id=1, channel="wechat_personal", contact_id="wxid_a")
        b_conv = Conversation(tenant_id=2, channel="wechat_personal", contact_id="wxid_b")
        self.db.add_all([a_conv, b_conv])
        self.db.flush()
        self.db.add_all([
            Message(tenant_id=1, conversation_id=a_conv.id, direction="in", sender="customer",
                    provenance="customer", text="tenant-a-event", meta={}),
            Message(tenant_id=2, conversation_id=b_conv.id, direction="in", sender="customer",
                    provenance="customer", text="tenant-b-secret", meta={}),
            CustomerMemory(tenant_id=1, channel="wechat_personal", contact_id="wxid_a",
                           memory_type="preference", content="喜欢蓝色", source="manual"),
            CustomerMemory(tenant_id=2, channel="wechat_personal", contact_id="wxid_b",
                           memory_type="fact", content="其他租户秘密", source="manual"),
        ])
        doc_a = KbDocument(tenant_id=1, title="A文档", source_type="faq", status="ready")
        doc_b = KbDocument(tenant_id=2, title="B文档", source_type="faq", status="ready")
        self.db.add_all([doc_a, doc_b])
        self.db.flush()
        self.db.add_all([
            KbChunk(tenant_id=1, document_id=doc_a.id, ord=0, text="A证据"),
            KbChunk(tenant_id=2, document_id=doc_b.id, ord=0, text="B秘密证据"),
        ])
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_overview_and_data_browser_are_tenant_scoped(self) -> None:
        registry = OpsTaskRegistry()
        task = registry.start(1, "memory.recall", "召回客户记忆")
        registry.finish(task, status="completed", detail="命中 2 条")
        other = registry.start(2, "profile.update", "其他租户任务")
        registry.finish(other, status="failed", detail="secret")

        overview = ops_overview(self.db, 1, registry=registry)
        messages = browse_dataset(self.db, 1, "messages", limit=20)
        memories = browse_dataset(self.db, 1, "memories", limit=20)

        self.assertEqual(1, overview["stats"]["events"])
        self.assertEqual(1, overview["stats"]["evidence"])
        self.assertEqual(1, overview["stats"]["long_term_memories"])
        self.assertEqual("memory.recall", overview["worker_tasks"][0]["kind"])
        self.assertEqual("tenant-a-event", messages["rows"][0]["text"])
        self.assertEqual("喜欢蓝色", memories["rows"][0]["content"])
        self.assertNotIn("tenant-b-secret", str(overview) + str(messages) + str(memories))

    def test_model_gateway_is_descriptive_and_never_contains_secrets(self) -> None:
        overview = ops_overview(self.db, 1, registry=OpsTaskRegistry())

        gateway = overview["model_gateway"]
        self.assertIn(gateway["provider"], {"fake", "dashscope", "deepseek"})
        self.assertTrue(gateway["chat_model"])
        self.assertTrue(gateway["embedding_model"])
        self.assertNotIn("api_key", str(gateway).lower())

    def test_worker_task_registry_deletion_is_tenant_scoped(self) -> None:
        registry = OpsTaskRegistry()
        first = registry.start(1, "memory.ingest", "租户一任务")
        second = registry.start(1, "memory.recall", "租户一任务二")
        foreign = registry.start(2, "memory.ingest", "其他租户任务")

        deleted = registry.delete(1, [first, foreign])

        self.assertEqual(1, deleted)
        self.assertEqual([second], [row["id"] for row in registry.recent(1)])
        self.assertEqual([foreign], [row["id"] for row in registry.recent(2)])

    def test_data_browser_message_deletion_is_tenant_scoped(self) -> None:
        own = self.db.query(Message).filter(Message.tenant_id == 1).one()
        foreign = self.db.query(Message).filter(Message.tenant_id == 2).one()

        result = delete_dataset_rows(
            self.db, 1, "messages", [own.id, foreign.id],
        )

        self.assertEqual(1, result["deleted"])
        self.assertIsNone(self.db.get(Message, own.id))
        self.assertIsNotNone(self.db.get(Message, foreign.id))

    def test_data_browser_conversation_deletion_removes_owned_children_only(self) -> None:
        own = self.db.query(Conversation).filter(Conversation.tenant_id == 1).one()
        foreign = self.db.query(Conversation).filter(Conversation.tenant_id == 2).one()
        self.db.add(Deal(
            tenant_id=1, conversation_id=own.id, contact_id="wxid_a",
            channel="wechat_personal",
        ))
        self.db.commit()

        result = delete_dataset_rows(self.db, 1, "conversations", [own.id, foreign.id])

        self.assertEqual(1, result["deleted"])
        self.assertIsNone(self.db.get(Conversation, own.id))
        self.assertEqual(0, self.db.query(Message).filter(Message.conversation_id == own.id).count())
        self.assertEqual(0, self.db.query(Deal).filter(Deal.conversation_id == own.id).count())
        self.assertIsNotNone(self.db.get(Conversation, foreign.id))

    def test_data_browser_memory_and_knowledge_use_domain_deletion(self) -> None:
        memory = self.db.query(CustomerMemory).filter(CustomerMemory.tenant_id == 1).one()
        document = self.db.query(KbDocument).filter(KbDocument.tenant_id == 1).one()
        memory_service = MagicMock()
        memory_service.delete_memory.return_value = True

        memory_result = delete_dataset_rows(
            self.db, 1, "memories", [memory.id], memory_service=memory_service,
        )
        with patch("app.ops.service.kb_crud.delete_document") as delete_document:
            knowledge_result = delete_dataset_rows(
                self.db, 1, "knowledge", [document.id],
            )

        self.assertEqual(1, memory_result["deleted"])
        memory_service.delete_memory.assert_called_once_with(
            self.db, tenant_id=1, memory_id=memory.id,
        )
        self.assertEqual(1, knowledge_result["deleted"])
        delete_document.assert_called_once_with(self.db, 1, document.id)


class _WorkbenchBridge:
    def list_memories(self, **_kwargs):
        return []

    def memory_stats(self):
        return {"total": 0, "pinned": 0, "by_type": {}}


class _Controller:
    def pending(self):
        return []


class OpsNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_workbench_exposes_memory_and_operations_sections(self) -> None:
        window = WorkbenchWindow(
            RuntimeState(WidgetConfig()), _Controller(), _WorkbenchBridge(), adapter=None,
        )
        try:
            for name in (
                "memory_recall", "long_term_memory", "memory_governance",
                "worker_tasks", "data_browser", "functional_tests", "model_gateway",
            ):
                self.assertIn(name, window._idx)
        finally:
            window.close()
            window.deleteLater()

    def test_memory_pages_do_not_repeat_the_workbench_header_inside_the_page(self) -> None:
        window = WorkbenchWindow(
            RuntimeState(WidgetConfig()), _Controller(), _WorkbenchBridge(), adapter=None,
        )
        try:
            pages = (
                window.memory_conversation_page,
                window.long_term_memory_page,
                window.memory_page,
                window.memory_governance_page,
            )
            for page in pages:
                self.assertEqual([], page.findChildren(QLabel, "OpsTitle"))
        finally:
            window.close()
            window.deleteLater()

    def test_worker_tasks_support_selective_and_select_all_deletion(self) -> None:
        bridge = MagicMock()
        bridge.delete_ops_tasks.side_effect = lambda ids: {"deleted": len(ids)}
        page = WorkerTasksPage(bridge)
        rows = [
            {"id": "task-a", "kind": "memory.ingest", "label": "任务A"},
            {"id": "task-b", "kind": "memory.recall", "label": "任务B"},
        ]
        try:
            page._apply({"worker_tasks": rows})
            page._table.item(0, 0).setCheckState(Qt.CheckState.Checked)
            page._start = lambda callback, completed, _failed: (completed(callback()), True)[1]
            page.refresh = MagicMock()
            with patch(
                "widget.ui.pages.operations_page.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                page._delete_selected.click()
            bridge.delete_ops_tasks.assert_called_once_with(["task-a"])

            bridge.delete_ops_tasks.reset_mock()
            page._apply({"worker_tasks": rows})
            page._select_all.click()
            with patch(
                "widget.ui.pages.operations_page.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                page._delete_selected.click()
            bridge.delete_ops_tasks.assert_called_once_with(["task-a", "task-b"])
        finally:
            page.close(); page.deleteLater()

    def test_data_browser_supports_selective_and_select_all_deletion(self) -> None:
        bridge = MagicMock()
        bridge.delete_ops_data.side_effect = lambda dataset, ids: {
            "dataset": dataset, "deleted": len(ids),
        }
        page = DataBrowserPage(bridge)
        payload = {
            "dataset": "messages",
            "columns": ["id", "text"],
            "rows": [{"id": 11, "text": "一"}, {"id": 12, "text": "二"}],
        }
        try:
            page._apply(payload)
            page._table.item(1, 0).setCheckState(Qt.CheckState.Checked)
            page._start = lambda callback, completed, _failed: (completed(callback()), True)[1]
            page.refresh = MagicMock()
            with patch(
                "widget.ui.pages.operations_page.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                page._delete_selected.click()
            bridge.delete_ops_data.assert_called_once_with("messages", [12])

            bridge.delete_ops_data.reset_mock()
            page._apply(payload)
            page._select_all.click()
            with patch(
                "widget.ui.pages.operations_page.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                page._delete_selected.click()
            bridge.delete_ops_data.assert_called_once_with("messages", [11, 12])
        finally:
            page.close(); page.deleteLater()

    def test_memory_governance_has_one_memory_delete_button_named_delete(self) -> None:
        page = MemoryRecordsPage(MagicMock(), governance=True)
        try:
            buttons = [button.text() for button in page.findChildren(QPushButton)]
            self.assertEqual(1, buttons.count("删除"))
            self.assertEqual("删除", page._memory_delete_selected.text())
        finally:
            page.close(); page.deleteLater()


if __name__ == "__main__":
    unittest.main()
