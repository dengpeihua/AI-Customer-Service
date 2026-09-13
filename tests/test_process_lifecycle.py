from __future__ import annotations

from pathlib import Path

from widget.process_lifecycle import is_product_service_process, stop_product_services


ROOT = Path(__file__).resolve().parents[1]


def test_matches_only_this_checkout_background_runners() -> None:
    backend = f'"{ROOT / "runtime/python/python.exe"}" "{ROOT / "scripts/run_backend.py"}"'
    mem0 = f'"{ROOT / "mem/.venv/Scripts/python.exe"}" "{ROOT / "scripts/run_mem0.py"}"'

    assert is_product_service_process(backend, str(ROOT / "runtime/python/python.exe"), ROOT)
    assert is_product_service_process(mem0, str(ROOT / "mem/.venv/Scripts/python.exe"), ROOT)


def test_does_not_match_browser_or_another_checkout() -> None:
    widget = f'"{ROOT / "runtime/python/pythonw.exe"}" "{ROOT / "run_widget.py"}"'
    browser = r'"C:\Program Files\Google\Chrome\Application\chrome.exe"'
    other = r'"D:\AI-Customer-Service\runtime\python\python.exe" D:\AI-Customer-Service\scripts\run_backend.py'
    sibling = (
        f'"{ROOT}-old\\runtime\\python\\python.exe" '
        f'"{ROOT}-old\\scripts\\run_backend.py"'
    )

    assert not is_product_service_process(widget, str(ROOT / "runtime/python/pythonw.exe"), ROOT)
    assert not is_product_service_process(
        browser, r"C:\Program Files\Google\Chrome\Application\chrome.exe", ROOT,
    )
    assert not is_product_service_process(other, r"D:\AI-Customer-Service\runtime\python\python.exe", ROOT)
    assert not is_product_service_process(sibling, f"{ROOT}-old\\runtime\\python\\python.exe", ROOT)


def test_stop_terminates_only_matching_services(monkeypatch) -> None:
    class Process:
        def __init__(self, pid: int, command: str, executable: str) -> None:
            self.pid = pid
            self.info = {"cmdline": command.split(), "exe": executable}
            self.terminated = False

        def terminate(self) -> None:
            self.terminated = True

    backend = Process(
        101,
        f'{ROOT / "runtime/python/python.exe"} {ROOT / "scripts/run_backend.py"}',
        str(ROOT / "runtime/python/python.exe"),
    )
    browser = Process(
        102,
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    )
    widget = Process(
        103,
        f'{ROOT / "runtime/python/pythonw.exe"} {ROOT / "run_widget.py"}',
        str(ROOT / "runtime/python/pythonw.exe"),
    )
    import psutil

    monkeypatch.setattr(psutil, "process_iter", lambda _fields: [backend, browser, widget])
    monkeypatch.setattr(psutil, "wait_procs", lambda processes, timeout: (processes, []))

    assert stop_product_services(ROOT, current_pid=999) == [101]
    assert backend.terminated
    assert not browser.terminated
    assert not widget.terminated
