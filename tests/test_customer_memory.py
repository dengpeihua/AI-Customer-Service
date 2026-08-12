from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QLabel
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401  register all tables
from app.crud.memory import (create_memory, delete_memory, list_contact_memories,
                             list_memories, memory_stats, update_memory,
                             upsert_profile_memory)
from app.db import Base
from widget.ui.pages.memory_page import MemoryPage
from widget.ui.theme import apply_theme


class CustomerMemoryCrudTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_memories_are_tenant_scoped_ranked_and_editable(self) -> None:
        low = create_memory(
            self.db, 1, channel="wechat_personal", contact_id="wxid_a",
            memory_type="note", content="普通备注", source="manual", source_key=None,
            importance=0.2, is_pinned=False,
        )
        create_memory(
            self.db, 1, channel="wechat_personal", contact_id="wxid_a",
            memory_type="preference", content="偏好深蓝色", source="manual", source_key=None,
            importance=0.9, is_pinned=True,
        )
        create_memory(
            self.db, 2, channel="wechat_personal", contact_id="wxid_a",
            memory_type="fact", content="其他租户", source="manual", source_key=None,
            importance=1.0, is_pinned=True,
        )

        rows = list_memories(self.db, 1, search="wxid_a")
        self.assertEqual(["偏好深蓝色", "普通备注"], [row.content for row in rows])
        self.assertEqual(2, memory_stats(self.db, 1)["total"])

        changed = update_memory(self.db, 1, low.id, content="已更新", importance=0.7)
        self.assertEqual("已更新", changed.content)
        self.assertIsNone(update_memory(self.db, 2, low.id, content="越权"))
        self.assertFalse(delete_memory(self.db, 2, low.id))
        self.assertTrue(delete_memory(self.db, 1, low.id))

    def test_profile_memory_is_idempotent_and_available_to_dialog(self) -> None:
        first = upsert_profile_memory(
            self.db, 1, "wecom_hook", "customer-1", "首次画像"
        )
        second = upsert_profile_memory(
            self.db, 1, "wecom_hook", "customer-1", "更新后的画像"
        )

        self.assertEqual(first.id, second.id)
        rows = list_contact_memories(self.db, 1, "wecom_hook", "customer-1")
        self.assertEqual(1, len(rows))
        self.assertEqual("更新后的画像", rows[0].content)


class _MemoryBridge:
    def memory_workbench(self, **_kwargs):
        question = {
            "index": 0, "question": "What does Caroline prefer?", "answer": "Painting",
            "category": "single-hop", "evidence": ["D1:2"],
        }
        return {
            "dataset": {"name": "LoCoMo-10", "sample_id": "locomo_0", "case_index": 0, "case_count": 1},
            "cases": [{"index": 0, "label": "案例 1 · locomo_0"}],
            "speakers": {"a": "Caroline", "b": "Melanie"},
            "sessions": [{"index": 1, "date_time": "8 May 2023", "message_count": 1}],
            "selected_session": 1,
            "messages": [{
                "id": "D1:1", "speaker": "Caroline", "role": "user",
                "text": "I enjoy painting.", "session": 1, "date_time": "8 May 2023",
            }],
            "questions": [question],
            "selected_question": 0,
            "stats": {"events": 28, "evidence": 26, "worker_tasks": 26, "long_term_memories": 16},
            "preview_results": [{"id": "m1", "memory": "Caroline enjoys painting", "score": 0.91}],
            "mem0": {"status": "ok", "mode": "local-oss", "live": True, "collection": "locomo"},
            "runtime_overview": {
                "stats": {"events": 3, "evidence": 2, "worker_tasks": 1, "long_term_memories": 4},
            },
        }


class MemoryPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_page_renders_locomo_stats_messages_and_recall_preview(self) -> None:
        page = MemoryPage(
            _MemoryBridge(), labels={"wechat_personal": "个人微信"}
        )
        try:
            page.refresh()
            self.assertEqual("3", page._stat_values["events"].text())
            self.assertEqual("4", page._stat_values["long_term_memories"].text())
            self.assertEqual(1, len(page._base_messages))
            self.assertEqual("I enjoy painting.", page._base_messages[0]["text"])
            self.assertEqual("1", page._result_badge.text())
            self.assertIn("What does Caroline prefer?", page._query.toPlainText())
        finally:
            page.close()
            page.deleteLater()

    def test_page_uses_light_surfaces_with_dark_blue_text(self) -> None:
        apply_theme(self.app)
        page = MemoryPage(_MemoryBridge(), labels={"wechat_personal": "个人微信"})
        try:
            page.refresh()
            page.show()
            self.app.processEvents()
            surface = page.palette().color(QPalette.ColorRole.Window)
            self.assertGreater(min(surface.red(), surface.green(), surface.blue()), 220)

            for object_name in (
                "MemoryPageTitle", "MemoryMuted", "MemoryBubbleText", "MemoryRecallText",
            ):
                label = page.findChild(QLabel, object_name)
                self.assertIsNotNone(label, object_name)
                color = label.palette().color(QPalette.ColorRole.WindowText)
                self.assertLess(
                    color.red() + color.green() + color.blue(),
                    360,
                    f"{object_name} should use dark-blue text, got {color.name()}",
                )
        finally:
            page.close()
            page.deleteLater()


if __name__ == "__main__":
    unittest.main()
