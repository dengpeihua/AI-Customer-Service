"""Run the focused regression suite without requiring pytest.

Production dependencies still need to be installed. This keeps the tests runnable
inside the production container, where pytest is intentionally not installed.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
import traceback
import unittest
from pathlib import Path


TEST_FILES = (
    "test_conversation_memory.py",
    "test_agent_orchestrator.py",
    "test_rag_deduplication.py",
    "test_performance_monitor.py",
    "test_structured_llm_budget.py",
)


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载测试模块: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    root = Path(__file__).resolve().parent
    project_root = root.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    failures = 0
    total = 0

    for filename in TEST_FILES:
        module = load_module(root / filename)

        suite = unittest.defaultTestLoader.loadTestsFromModule(module)
        if suite.countTestCases():
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            total += result.testsRun
            failures += len(result.failures) + len(result.errors)

        for name, test_func in inspect.getmembers(module, inspect.isfunction):
            if not name.startswith("test_"):
                continue
            total += 1
            try:
                test_func()
            except Exception:
                failures += 1
                print(f"FAIL: {filename}::{name}")
                traceback.print_exc()
            else:
                print(f"PASS: {filename}::{name}")

    print(f"Focused tests: {total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
