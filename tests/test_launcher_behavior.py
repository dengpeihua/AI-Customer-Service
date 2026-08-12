from __future__ import annotations

from pathlib import Path
import inspect
import unittest

from widget.app import run as run_widget


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "Start-AICustomerService.ps1"
RUNTIME_RESTART = ROOT / "scripts" / "Restart-AIRuntime.ps1"


class LauncherBehaviorTests(unittest.TestCase):
    def test_account_picker_precedes_any_startup_workbench_window(self):
        source = inspect.getsource(run_widget)

        picker = source.index("prepare_legacy_wechat_selection")
        startup_window = source.index("startup.show()")
        self.assertLess(picker, startup_window)

    def test_widget_start_is_not_blocked_by_hook_login_wait(self):
        script = LAUNCHER.read_text(encoding="utf-8")

        # The helper may remain for explicit diagnostics, but normal startup must
        # not call it before showing the workbench.
        self.assertEqual(1, script.count("Wait-HookAfterLogin"))
        self.assertNotIn("AddMinutes(5)", script)
        self.assertIn('Write-Step "Start widget"\nStart-Widget', script)

    def test_workbench_starts_before_optional_mem0_warmup(self):
        script = LAUNCHER.read_text(encoding="utf-8")

        backend = script.index('Write-Step "Start backend"')
        widget = script.index('Write-Step "Start widget"')
        mem0 = script.index('Write-Step "Start memory engine"')
        self.assertLess(backend, widget)
        self.assertLess(widget, mem0)

    def test_memory_engine_startup_copy_uses_generic_product_name(self):
        script = LAUNCHER.read_text(encoding="utf-8")

        self.assertIn('Write-Step "Start memory engine"', script)
        self.assertIn(
            'Write-Host "Mem engine already running: http://127.0.0.1:8888"',
            script,
        )
        self.assertIn(
            'Write-Host "Mem engine started: http://127.0.0.1:8888"',
            script,
        )
        self.assertIn("Memory engine did not become healthy", script)
        self.assertNotIn("Start Mem0 memory engine", script)
        self.assertNotIn("Mem0 already running", script)
        self.assertNotIn("Mem0 started", script)
        self.assertNotIn("Mem0 did not become healthy", script)

    def test_initial_session_capture_starts_before_polling_baseline(self):
        source = inspect.getsource(run_widget)

        polling = source.index("hub.start_all(on_msg)")
        self.assertIn("wb.history_page.live_tick()", source[polling - 600:polling])

    def test_backend_probe_is_fast_and_running_process_is_reloaded(self):
        script = LAUNCHER.read_text(encoding="utf-8")

        self.assertIn("Wait-Backend -Attempts 1", script)
        self.assertIn("if ($i -lt ($Attempts - 1))", script)
        self.assertIn('scripts\\run_backend.py', script)
        self.assertIn("backend already running; reloading current code/config", script)
        self.assertNotIn("backend already running with current configuration", script)
        self.assertIn('Join-Path $ScriptDir "Restart-AIBackend.ps1"', script)

    def test_runtime_restart_can_identify_elevated_widget_without_command_line(self):
        script = RUNTIME_RESTART.read_text(encoding="utf-8")

        self.assertIn('$process.MainWindowTitle -in @("AI客服", "AI 客服工作台")', script)
        self.assertIn('ACS_WIDGET_LOG_DIR', script)

    def test_runtime_restart_finds_console_and_windowless_widget_hosts(self):
        script = RUNTIME_RESTART.read_text(encoding="utf-8")

        self.assertIn('$_.Name -in @("python.exe", "pythonw.exe")', script)
        self.assertNotIn('Win32_Process -Filter "Name=\'pythonw.exe\'"', script)

    def test_mem0_reuse_requires_exact_ascii_project_data_path(self):
        script = LAUNCHER.read_text(encoding="utf-8")

        self.assertIn('$expectedData = [IO.Path]::GetFullPath((Join-Path $Root "mem\\data"))', script)
        self.assertIn('$actualData.Equals(', script)
        self.assertIn('$existing.mode -eq "local-oss"', script)
        self.assertIn('$existing.collection -eq "memories_dashscope_1024"', script)
        self.assertIn('$existing.telemetry -eq $false', script)
        self.assertIn('$existing.service_revision -eq "acs-mem0-companion-v4-structured-retry"', script)
        self.assertIn('if ($isProjectData -and -not $isExpectedRuntime)', script)
        self.assertNotIn('Join-Path $Root "mem\\data("', script)

if __name__ == "__main__":
    unittest.main()
