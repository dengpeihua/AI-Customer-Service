from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path


class MemDirectoryLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.outer = cls.root / "mem"
        cls.package = cls.outer / "mem"

    def test_old_directories_are_replaced_by_requested_mem_directories(self) -> None:
        self.assertTrue(self.outer.is_dir())
        self.assertTrue(self.package.is_dir())
        self.assertTrue((self.package / "__init__.py").is_file())
        self.assertFalse((self.root / "mem0").exists())
        self.assertFalse((self.outer / "mem0").exists())
        self.assertTrue((self.outer / "data").is_dir())

    def test_launchers_and_package_metadata_use_new_local_paths(self) -> None:
        runner = (self.root / "scripts" / "run_mem0.py").read_text(encoding="utf-8")
        launcher = (self.root / "scripts" / "Start-AICustomerService.ps1").read_text(encoding="utf-8")
        pyproject = (self.outer / "pyproject.toml").read_text(encoding="utf-8")
        server = (self.outer / "server" / "main.py").read_text(encoding="utf-8")
        locomo = (self.root / "app" / "memory" / "locomo.py").read_text(encoding="utf-8")

        self.assertIn('MEM0_ROOT = ROOT / "mem"', runner)
        self.assertIn('"mem\\.venv\\Scripts\\python.exe"', launcher)
        self.assertIn('Join-Path $Root "mem\\data"', launcher)
        self.assertIn('packages = ["mem"]', pyproject)
        self.assertIn("from mem import Memory, __version__", server)
        self.assertIn('PROJECT_ROOT / "mem" / "data"', locomo)
        self.assertNotIn('PROJECT_ROOT / "mem0" / "data"', locomo)

    def test_python_sources_no_longer_import_old_mem0_package(self) -> None:
        old_import = re.compile(r"^(?:from|import)\s+mem0(?:\.|\s|$)", re.MULTILINE)
        offenders = []
        for base in (self.package, self.outer / "server", self.outer / "tests"):
            for path in base.rglob("*.py"):
                if old_import.search(path.read_text(encoding="utf-8")):
                    offenders.append(str(path.relative_to(self.root)))
        self.assertEqual([], offenders)

    def test_moved_virtualenv_imports_local_mem_package(self) -> None:
        python = self.outer / ".venv" / "Scripts" / "python.exe"
        self.assertTrue(python.is_file())
        code = (
            "import pathlib,sys; "
            f"root=pathlib.Path({str(self.outer)!r}); "
            "sys.path.insert(0,str(root)); import mem; from mem.memory.main import Memory; "
            "print(pathlib.Path(mem.__file__).resolve())"
        )
        completed = subprocess.run(
            [str(python), "-c", code], cwd=self.root,
            capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(
            (self.package / "__init__.py").resolve(),
            Path(completed.stdout.strip()).resolve(),
        )

    def test_embedded_environment_points_at_moved_data_directory(self) -> None:
        data_line = next(
            line for line in (self.outer / ".env").read_text(encoding="utf-8").splitlines()
            if line.startswith("MEM0_DATA_DIR=")
        )
        normalized = data_line.split("=", 1)[1].replace("\\", "/")
        self.assertTrue(normalized.endswith("/AI-Customer-Service/mem/data"))


if __name__ == "__main__":
    unittest.main()
