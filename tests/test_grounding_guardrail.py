from __future__ import annotations

import unittest

from app.dialog.engine import _reply_requires_handoff


class GroundingGuardrailTests(unittest.TestCase):
    def test_explicit_marker_requires_handoff(self):
        self.assertTrue(_reply_requires_handoff("<转人工>"))

    def test_confirmation_language_requires_handoff(self):
        self.assertTrue(_reply_requires_handoff("这个具体日期我帮您确认一下。"))

    def test_grounded_answer_can_auto_reply(self):
        self.assertFalse(_reply_requires_handoff("付款后 48 小时内发货。"))

    def test_empty_reply_requires_handoff(self):
        self.assertTrue(_reply_requires_handoff("  "))

if __name__ == "__main__":
    unittest.main()
