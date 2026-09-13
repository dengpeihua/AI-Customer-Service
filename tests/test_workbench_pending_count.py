from __future__ import annotations

import os
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel

from widget.config import WidgetConfig
from widget.handoff import HandoffController
from widget.state import RuntimeState
from widget.ui.pages.handoff_page import HandoffPage
from widget.ui.workbench import WorkbenchWindow


class _Adapter:
    def send_message(self, _contact: str, _text: str, provenance: str):
        return SimpleNamespace(ok=provenance == "human")


class _Hub:
    def adapter(self, _channel=None):
        return _Adapter()

    def pipeline(self, _channel=None):
        return SimpleNamespace(release_contact=lambda _contact: None)


class WorkbenchPendingCountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_manual_reply_clears_navigation_badge(self):
        state = RuntimeState(WidgetConfig())
        state.add_pending(
            {
                "channel": "douyin#shop_a",
                "msg_id": "test-message",
                "contact_id": "wxid_customer",
                "sender_id": "wxid_customer",
                "text": "需要人工处理",
                "is_group": False,
                "at_me": False,
                "timestamp": 1,
            },
            {"reply_text": "人工草稿"},
        )
        controller = HandoffController(_Hub(), state)
        window = WorkbenchWindow(state, controller, bridge=object(), adapter=None)
        try:
            window.refresh()
            self.assertEqual("待人工 (1)", window._btn["handoff"].text())

            self.assertTrue(
                controller.reply("wxid_customer", "已人工回复", "douyin#shop_a")
            )
            QApplication.processEvents()

            self.assertEqual([], state.pending)
            self.assertEqual("待人工", window._btn["handoff"].text())
            self.assertEqual(0, window.handoff_page.pending_count())
        finally:
            window.close()
            window.deleteLater()

    def test_product_name_is_ai_customer_service_everywhere_in_workbench(self):
        state = RuntimeState(WidgetConfig())
        controller = HandoffController(_Hub(), state)
        window = WorkbenchWindow(state, controller, bridge=object(), adapter=None)
        try:
            brand = window.findChild(QLabel, "BrandName")
            self.assertIsNotNone(brand)
            self.assertEqual("AI客服", brand.text())
            self.assertEqual("AI客服", window.windowTitle())
        finally:
            window.close()
            window.deleteLater()

    def test_returning_to_history_uses_non_destructive_background_activation(self):
        state = RuntimeState(WidgetConfig())
        controller = HandoffController(_Hub(), state)
        window = WorkbenchWindow(state, controller, bridge=object(), adapter=None)
        try:
            window.history_page.refresh = Mock()
            window.history_page.activate = Mock()
            window._switch(window._idx["status"])
            window._switch(window._idx["history"])

            window.history_page.refresh.assert_not_called()
            window.history_page.activate.assert_called_once_with()
        finally:
            window.close()
            window.deleteLater()

    def test_handoff_reply_does_not_block_the_gui_thread(self):
        class BlockingController:
            def __init__(self):
                self.item = {
                    "id": "pending-1",
                    "channel": "douyin#shop_a",
                    "contact": "customer-1",
                    "text": "需要人工回复",
                    "draft": "",
                }
                self.started = threading.Event()
                self.release = threading.Event()
                self.changed = 0
                self.last_error = ""

            def pending(self):
                return [dict(self.item)] if self.item else []

            def reply_pending(self, pending_id, text, *, notify=True):
                self.started.set()
                self.release.wait(timeout=2)
                self.item = None
                return pending_id == "pending-1" and text == "人工回复"

            def notify_changed(self):
                self.changed += 1

        controller = BlockingController()
        page = HandoffPage(controller)
        try:
            page._edit.setPlainText("人工回复")
            started_at = time.monotonic()
            page._on_send()
            elapsed = time.monotonic() - started_at

            self.assertLess(elapsed, 0.1)
            self.assertTrue(controller.started.wait(timeout=1))
            self.assertFalse(page._send_button.isEnabled())
            self.assertIn("正在发送", page._status.text())

            controller.release.set()
            deadline = time.monotonic() + 2
            while not page._send_button.isEnabled() and time.monotonic() < deadline:
                QApplication.processEvents()
                time.sleep(0.01)

            self.assertTrue(page._send_button.isEnabled())
            self.assertEqual("已回复并结束这一条待办", page._status.text())
            self.assertEqual(1, controller.changed)
        finally:
            controller.release.set()
            page.close()
            page.deleteLater()


if __name__ == "__main__":
    unittest.main()
