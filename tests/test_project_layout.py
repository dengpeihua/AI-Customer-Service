from __future__ import annotations

import os
from pathlib import Path

import agents
from agents.sandbox.memory.prompts import (
    MEMORY_CONSOLIDATION_PROMPT_TEMPLATE,
    MEMORY_READ_PROMPT_TEMPLATE,
    ROLLOUT_EXTRACTION_PROMPT_TEMPLATE,
    ROLLOUT_EXTRACTION_USER_MESSAGE_TEMPLATE,
)
from agents.sandbox.runtime_agent_preparation import get_default_sandbox_instructions


ROOT = Path(__file__).resolve().parents[1]


def test_vendored_agents_uses_the_compact_directory() -> None:
    assert not (ROOT / "openai-agents").exists()
    assert (ROOT / "agents" / "src" / "agents").is_dir()

    expected_package = (ROOT / "agents" / "src" / "agents").resolve()
    assert Path(agents.__file__).resolve().parent == expected_package

    for requirements_name in ("requirements.txt", "requirements.lock.txt"):
        requirements = (ROOT / requirements_name).read_text(encoding="utf-8").splitlines()
        assert "-e ./agents" in requirements
        assert "-e ./openai-agents" not in requirements


def test_runtime_prompts_survive_markdown_cleanup() -> None:
    prompts = (
        get_default_sandbox_instructions(),
        MEMORY_CONSOLIDATION_PROMPT_TEMPLATE,
        MEMORY_READ_PROMPT_TEMPLATE,
        ROLLOUT_EXTRACTION_PROMPT_TEMPLATE,
        ROLLOUT_EXTRACTION_USER_MESSAGE_TEMPLATE,
    )
    assert all(isinstance(prompt, str) and len(prompt) > 100 for prompt in prompts)

    prompt_root = ROOT / "agents" / "src" / "agents" / "sandbox"
    assert (prompt_root / "instructions" / "prompt.txt").is_file()
    assert not (prompt_root / "instructions" / "prompt.md").exists()
    assert not list((prompt_root / "memory" / "prompts").glob("*.md"))


def test_project_owned_markdown_is_consolidated() -> None:
    excluded_roots = {
        (ROOT / "runtime").resolve(),
        (ROOT / "mem" / ".python").resolve(),
        (ROOT / "mem" / ".venv").resolve(),
    }
    markdown: set[Path] = set()
    for current, directories, files in os.walk(ROOT, topdown=True):
        current_path = Path(current).resolve()
        directories[:] = [
            name
            for name in directories
            if (current_path / name).resolve() not in excluded_roots
            and name not in {".pytest_cache", "__pycache__"}
        ]
        markdown.update(
            (current_path / name).resolve() for name in files if name.lower().endswith(".md")
        )

    assert markdown == {
        (ROOT / "AGENTS.md").resolve(),
        (ROOT / "README.md").resolve(),
    }
