from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from app.ops.service import run_functional_checks
from widget.config import WidgetConfig
from widget.state import RuntimeState
from widget.ui.pages.history_page import HistoryPage
from widget.ui.pages.long_term_memory_page import LongTermMemoryPage
from widget.ui.pages.memory_conversation_page import MemoryConversationPage
from widget.ui.pages.memory_recall_page import MemoryRecallPage
from widget.ui.pages.operations_page import FunctionalTestsPage
from widget.ui.workbench import WorkbenchWindow


EXPECTED_MEMORY_GUIDE = (
    "真实微信对话→提炼长期记忆→客户下次发消息时按当前话题语义召回→只把相关记忆交给客服模型→"
    "生成有连续感的客服回复。事实：稳定背景；偏好：沟通或选择倾向；需求：正在推进的目标；"
    "承诺：客户或客服明确约定的后续；备注：人工确认的补充信息。不相关记忆不会整库注入。"
    "当前自动注入范围是非业务闲聊或情绪支持；涉及产品、售后、投诉等使用知识库，缺少依据就转人工。"
)


class RequestedUiCopyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_customer_reception_switch_is_above_ai_switch_with_exact_copy(self) -> None:
        page = HistoryPage(
            MagicMock(), adapter=None, state=RuntimeState(WidgetConfig()),
            defer_initial_load=True,
        )
        try:
            page.resize(1000, 700)
            page.show()
            QApplication.processEvents()

            self.assertEqual(
                "接待此客户(关=不进AI、不留后台记录、不弹窗)",
                page._monitor_switch.text(),
            )
            self.assertEqual(
                "AI托管(关=转人工，开=允许AI处理)",
                page._ai_switch.text(),
            )
            self.assertLess(page._monitor_switch.y(), page._ai_switch.y())
        finally:
            page.close()
            page.deleteLater()

    def test_conversation_center_uses_manual_kb_admin_button_copy(self) -> None:
        page = HistoryPage(
            MagicMock(), adapter=None, state=RuntimeState(WidgetConfig()),
            defer_initial_load=True,
        )
        try:
            self.assertIn(
                "后台管理知识库",
                [button.text() for button in page.findChildren(QPushButton)],
            )
            self.assertNotIn(
                "总结并更新知识库/话术库",
                [button.text() for button in page.findChildren(QPushButton)],
            )
        finally:
            page.close()
            page.deleteLater()

    def test_conversation_center_kb_button_opens_admin_without_summarizing(self) -> None:
        bridge = MagicMock()
        bridge.cfg = SimpleNamespace(backend_base_url="http://127.0.0.1:8000/")
        page = HistoryPage(
            bridge, adapter=None, state=RuntimeState(WidgetConfig()),
            defer_initial_load=True,
        )
        try:
            button = next(
                child for child in page.findChildren(QPushButton)
                if child.text() == "后台管理知识库"
            )
            with patch(
                "widget.ui.pages.history_page.QDesktopServices.openUrl",
                return_value=True,
            ) as open_url:
                button.click()

            opened_url = open_url.call_args.args[0]
            self.assertEqual(
                "http://127.0.0.1:8000/admin/kb",
                opened_url.toString(),
            )
            bridge.summarize_to_kb.assert_not_called()
        finally:
            page.close()
            page.deleteLater()

    def test_conversation_center_kb_button_reports_browser_open_failure(self) -> None:
        bridge = MagicMock()
        bridge.cfg = SimpleNamespace(backend_base_url="http://127.0.0.1:8000")
        page = HistoryPage(
            bridge, adapter=None, state=RuntimeState(WidgetConfig()),
            defer_initial_load=True,
        )
        try:
            with (
                patch(
                    "widget.ui.pages.history_page.QDesktopServices.openUrl",
                    return_value=False,
                ),
                patch("widget.ui.pages.history_page.QMessageBox.warning") as warning,
            ):
                page._kb_admin_button.click()

            warning.assert_called_once()
            self.assertIn(
                "http://127.0.0.1:8000/admin/kb",
                warning.call_args.args[2],
            )
            bridge.summarize_to_kb.assert_not_called()
        finally:
            page.close()
            page.deleteLater()

    def test_memory_conversation_uses_requested_guide_and_button_copy(self) -> None:
        page = MemoryConversationPage(MagicMock(), adapter=None)
        try:
            self.assertEqual(EXPECTED_MEMORY_GUIDE, page._memory_usage.text())
            self.assertEqual("提取长期记忆", page._ingest.text())
        finally:
            page.close()
            page.deleteLater()

    def test_long_term_memory_uses_requested_profile_and_create_copy(self) -> None:
        page = LongTermMemoryPage(MagicMock())
        try:
            page._apply_profile({"display_name": "测试客户", "memories": [], "sync": {}})
            self.assertEqual("测试客户 · 长期画像", page._profile_title.text())
            self.assertIn(
                "新增并同步到长期记忆",
                [button.text() for button in page.findChildren(QPushButton)],
            )
            self.assertEqual(
                "人工确认的长期记忆(会同步写入，参与真实召回)",
                page._manual_content.placeholderText(),
            )
        finally:
            page.close()
            page.deleteLater()

    def test_long_term_memory_header_uses_requested_description(self) -> None:
        controller = MagicMock()
        controller.pending.return_value = []
        bridge = MagicMock()
        bridge.list_memories.return_value = []
        bridge.memory_stats.return_value = {"total": 0, "pinned": 0, "by_type": {}}
        window = WorkbenchWindow(
            RuntimeState(WidgetConfig()), controller, bridge, adapter=None,
        )
        try:
            window._goto("long_term_memory")
            self.assertEqual(
                "查看由长期记忆形成的用户画像",
                window._header_sub.text(),
            )
        finally:
            window.close()
            window.deleteLater()

    def test_memory_recall_uses_requested_placeholder(self) -> None:
        page = MemoryRecallPage(MagicMock())
        try:
            self.assertEqual(
                "输入客户当前消息（这里只检索相关记忆）",
                page._query.placeholderText(),
            )
        finally:
            page.close()
            page.deleteLater()

    def test_functional_tests_use_requested_description_and_check_name(self) -> None:
        page = FunctionalTestsPage(MagicMock())
        try:
            label_texts = [label.text() for label in page.findChildren(QLabel)]
            self.assertIn("系统健康检查，不代表真实的长期记忆", label_texts)
        finally:
            page.close()
            page.deleteLater()

        memory_service = MagicMock()
        memory_service.gateway.health.return_value = {"status": "ok", "mode": "local"}
        with patch("app.memory.get_memory_workbench_service", return_value=memory_service):
            checks = run_functional_checks(MagicMock(), tenant_id=7)
        names = [row["name"] for row in checks]
        self.assertIn("记忆引擎", names)
        self.assertNotIn("Mem0 记忆引擎", names)

        with patch(
            "app.memory.get_memory_workbench_service", side_effect=RuntimeError("offline")
        ):
            offline_checks = run_functional_checks(MagicMock(), tenant_id=7)
        self.assertIn("记忆引擎", [row["name"] for row in offline_checks])


if __name__ == "__main__":
    unittest.main()
