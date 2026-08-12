import importlib.metadata
from pathlib import Path


def _source_version() -> str:
    """Return the checked-out source version when running without installation."""
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if pyproject.exists():
        for line in pyproject.read_text(encoding="utf-8").splitlines():
            if line.startswith("version = "):
                return line.split("=", 1)[1].strip().strip('"')
    return importlib.metadata.version("mem0ai")


__version__ = _source_version()

from mem.memory.main import AsyncMemory, Memory  # noqa

__all__ = ["Memory", "AsyncMemory", "__version__"]
