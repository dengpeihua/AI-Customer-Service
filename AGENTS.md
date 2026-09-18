# Repository Guidelines

## Project Structure & Module Organization

- `app/` contains the FastAPI backend, dialog orchestration, LLM adapters, RAG, and customer-memory services.
- `widget/` contains the PySide6 desktop workbench and personal Douyin channel adapters.
- `tests/` holds project-level regression tests. Name new files `test_<feature>.py`.
- `alembic/versions/` contains database migrations; never edit an applied revision; add a new one. `scripts/` contains launch, restart, and knowledge-base utilities.
- `agents/` and `mem/` are bundled components with their own `pyproject.toml` rules. `runtime/`, `native/`, and `assets/` are delivery/runtime resources, not general application source.
- `dataset/` contains reviewed knowledge-base inputs. Do not treat chat exports as approved business facts.

## Build, Test, and Development Commands

Run commands from the repository root in PowerShell:

```powershell
& .\scripts\Setup-AICustomerService.ps1
& .\.venv\Scripts\python.exe -m pytest .\tests -q
& .\.venv\Scripts\python.exe .\scripts\run_backend.py
& .\.venv\Scripts\python.exe .\run_widget.py
& .\scripts\Start-AICustomerService.ps1
```

The setup command rebuilds the ignored source environment after a GitHub clone. Bundled deliveries may use `runtime\python\python.exe` instead; the integrated launcher resolves either layout. The test command runs the root regression suite. For separate development, start the backend before the desktop UI and verify `http://127.0.0.1:8000/health`; the PowerShell launcher starts the integrated stack and applies migrations. To preview a read-only knowledge-base import, run `& .\.venv\Scripts\python.exe .\scripts\import_kb_dataset.py --dataset .\dataset`; add `--apply` only after review and backup.

## Coding Style & Naming Conventions

Use four-space indentation, explicit imports, and type annotations for changed Python APIs. Follow `snake_case` for modules/functions/variables, `PascalCase` for classes, and `UPPER_SNAKE_CASE` for constants. Preserve terminology used by the surrounding module. For bundled components, follow their `pyproject.toml` Ruff rules: `agents/` uses a 100-character line length and `mem/` uses 120.

## Testing Guidelines

Pytest discovers both pytest-style tests and existing `unittest.TestCase` suites. Add regression coverage for bug fixes and migration tests for schema changes. Run the narrowest relevant test during development, then the full root suite before submitting. No project-wide coverage threshold is configured.

## Commit & Pull Request Guidelines

This checkout does not include Git history, so no local convention can be verified. Use short, imperative commit subjects, optionally scoped, such as `fix(widget): preserve delivery lease`. Pull requests should explain behavior changes, list verification commands, link the issue, call out configuration or migration impact, and include screenshots for UI changes.

## Security & Configuration

Never commit `.env`, live `widget_config.yaml`, `douyin_accounts.yaml`, `acs.db`, backups, logs, tokens, cookies, Chrome profiles, or customer data. Copy `.env.example` and the `*.example.yaml` files for local setup; update templates only with safe placeholders. Back up `acs.db` before schema or knowledge-base writes. Keep `auto_send: false` in `widget_config.yaml` unless live personal-Douyin private-message sending is explicitly intended.
