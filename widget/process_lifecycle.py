"""Own and stop the background services launched with the desktop workbench."""
from __future__ import annotations

import os
from pathlib import Path


_SERVICE_RUNNERS = ("run_backend.py", "run_mem0.py")


def is_product_service_process(
    command_line: str,
    executable_path: str,
    project_root: str | Path,
) -> bool:
    """Return whether a process is one of this checkout's background services."""
    command = str(command_line or "")
    executable = str(executable_path or "")
    root = str(Path(project_root).resolve()).replace("/", "\\")
    command_folded = command.replace("/", "\\").casefold()
    executable_folded = executable.replace("/", "\\").casefold()
    root_prefix = root.rstrip("\\").casefold() + "\\"
    has_runner = any(runner in command_folded for runner in _SERVICE_RUNNERS)
    belongs_to_checkout = root_prefix in command_folded or executable_folded.startswith(root_prefix)
    return has_runner and belongs_to_checkout


def stop_product_services(project_root: str | Path, *, current_pid: int | None = None) -> list[int]:
    """Stop backend and Mem0 for this checkout, without touching external channel services."""
    import psutil

    own_pid = int(current_pid or os.getpid())
    matched = []
    for process in psutil.process_iter(["pid", "exe", "cmdline"]):
        try:
            if process.pid == own_pid:
                continue
            command = " ".join(str(part) for part in (process.info.get("cmdline") or []))
            executable = str(process.info.get("exe") or "")
            if not is_product_service_process(command, executable, project_root):
                continue
            process.terminate()
            matched.append(process)
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue

    _, alive = psutil.wait_procs(matched, timeout=3.0)
    for process in alive:
        try:
            process.kill()
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            pass
    return [process.pid for process in matched]
