from __future__ import annotations

import unittest

from app.config import settings
from app.routers.health import health


class HealthTests(unittest.TestCase):
    def test_reports_runtime_without_credentials(self):
        result = health()

        self.assertEqual("ok", result["status"])
        self.assertIn(f"provider={settings.llm_provider}", result["llm"])
        for credential in (
            settings.deepseek_api_key,
            settings.dashscope_api_key,
        ):
            if credential:
                self.assertNotIn(credential, result["llm"])


if __name__ == "__main__":
    unittest.main()
