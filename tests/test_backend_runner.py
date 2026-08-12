from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_backend.py"
RESTART = ROOT / "scripts" / "Restart-AIBackend.ps1"


class BackendRunnerTests(unittest.TestCase):
    def test_runner_makes_project_dotenv_authoritative(self):
        source = RUNNER.read_text(encoding="utf-8")

        self.assertIn('load_dotenv(ROOT / ".env", override=True)', source)
        self.assertLess(source.index("load_dotenv("), source.index("from app.llm import"))

    def test_normal_and_reload_launchers_share_runner(self):
        runner_name = "scripts\\run_backend.py"
        normal = (ROOT / "scripts" / "Start-AICustomerService.ps1").read_text(
            encoding="utf-8"
        )
        restart = RESTART.read_text(encoding="utf-8")

        self.assertIn(runner_name, normal)
        self.assertIn(runner_name, restart)
        self.assertNotIn('@("-m", "uvicorn", "app.main:app"', normal)
        self.assertNotIn('@("-m", "uvicorn", "app.main:app"', restart)


if __name__ == "__main__":
    unittest.main()
