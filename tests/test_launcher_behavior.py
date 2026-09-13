from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "Start-AICustomerService.ps1"
SHORTCUT_INSTALLER = ROOT / "scripts" / "Install-AIShortcut.ps1"


class LauncherBehaviorTests(unittest.TestCase):
    def test_launcher_is_valid_windows_powershell(self) -> None:
        powershell = shutil.which("powershell")
        if powershell is None:
            self.skipTest("Windows PowerShell is not installed")
        escaped_path = str(LAUNCHER).replace("'", "''")
        command = (
            "$tokens=$null; $errors=$null; "
            "[System.Management.Automation.Language.Parser]::ParseFile("
            f"'{escaped_path}', [ref]$tokens, [ref]$errors) | Out-Null; "
            "if ($errors.Count -gt 0) { "
            "$errors | ForEach-Object { [Console]::Error.WriteLine($_.Message) }; exit 1 }"
        )

        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)

    def test_shortcut_installer_targets_supported_batch_launcher(self) -> None:
        script = SHORTCUT_INSTALLER.read_text(encoding="utf-8")

        self.assertIn("$shortcut.TargetPath = $launcher", script)
        self.assertIn("$shortcut.WorkingDirectory = $ProjectRoot", script)
        self.assertIn("ai-customer-service.ico", script)

    def test_launcher_uses_douyin_profile_runtime(self) -> None:
        script = LAUNCHER.read_text(encoding="utf-8")
        self.assertIn("douyin_accounts.yaml", script)
        self.assertIn("AI.Customer.Service.Douyin.Launcher", script)
        self.assertNotIn("start_douyin_accounts.py", script)

    def test_workbench_starts_before_optional_services(self) -> None:
        script = LAUNCHER.read_text(encoding="utf-8")
        close_workbench = script.index('Write-Step "Close existing workbench"')
        backend = script.index('Write-Step "Start backend"')
        widget = script.index('Write-Step "Start widget"')
        memory = script.index('Write-Step "Start memory engine"')

        self.assertLess(close_workbench, widget)
        self.assertLess(widget, backend)
        self.assertLess(widget, memory)
        self.assertIn("Complete login in each opened Chrome profile", script)

    def test_elevated_launcher_closes_and_widget_owns_services(self) -> None:
        script = LAUNCHER.read_text(encoding="utf-8")

        self.assertNotIn("-NoExit", script)
        self.assertIn('$environment["ACS_MANAGE_SERVICES"] = "1"', script)
        self.assertIn("function Test-WidgetRunning", script)
        self.assertIn('Stop-ProductPython "run_backend.py"', script)
        self.assertIn("The workbench remains open in degraded mode", script)
        self.assertNotIn('throw "Backend did not become healthy', script)

    def test_memory_engine_reuse_requires_new_collection(self) -> None:
        script = LAUNCHER.read_text(encoding="utf-8")
        self.assertIn('"memories_minimax_embo_01_1536"', script)
        self.assertIn('"acs-mem0-companion-v5-resilient-dedupe"', script)
        self.assertNotIn("memories_dashscope_1024", script)

if __name__ == "__main__":
    unittest.main()
