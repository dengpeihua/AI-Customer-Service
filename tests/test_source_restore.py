from __future__ import annotations

import importlib.util
from pathlib import Path
import shutil
import sys

import pytest
import yaml

from widget import secret as widget_secret


ROOT = Path(__file__).resolve().parents[1]


def _load_configure_module():
    path = ROOT / "scripts" / "configure_local.py"
    spec = importlib.util.spec_from_file_location("configure_local_for_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_source_setup_creates_only_ignored_local_files(tmp_path: Path) -> None:
    configure = _load_configure_module()
    for relative in configure.LOCAL_TEMPLATES.values():
        source = ROOT / relative
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)

    created = configure.ensure_local_files(tmp_path)
    generated = configure.generate_application_secrets(tmp_path / ".env")

    assert {path.relative_to(tmp_path).as_posix() for path in created} == {
        ".env",
        "mem/.env",
        "widget_config.yaml",
        "douyin_accounts.yaml",
    }
    assert set(generated) == {"JWT_SECRET", "BOOTSTRAP_TOKEN", "FIELD_ENC_KEY"}
    _, values = configure._read_env(tmp_path / ".env")
    assert all(values[name] not in configure.PLACEHOLDERS for name in generated)
    assert values["MINIMAX_API_KEY"] == "待填写"
    assert values["DEEPSEEK_API_KEY"] == "待填写"
    assert (tmp_path / "mem" / "data").is_dir()


def test_source_templates_default_to_no_sending_and_no_plaintext_password() -> None:
    widget = yaml.safe_load((ROOT / "widget_config.example.yaml").read_text(encoding="utf-8"))
    accounts = yaml.safe_load((ROOT / "douyin_accounts.example.yaml").read_text(encoding="utf-8"))

    assert widget["password"] == ""
    assert widget["password_enc"] == ""
    assert widget["auto_send"] is False
    assert all(account["enabled"] is False for account in accounts["accounts"])
    assert all(account["send_enabled"] is False for account in accounts["accounts"])


def test_launchers_support_bundled_and_source_environments() -> None:
    runtime = (ROOT / "scripts" / "Runtime.ps1").read_text(encoding="utf-8")
    setup = (ROOT / "scripts" / "Setup-AICustomerService.ps1").read_text(encoding="utf-8")

    assert '"runtime\\python\\$executable"' in runtime
    assert '".venv\\Scripts\\$executable"' in runtime
    assert '"requirements.lock.txt"' in setup
    assert '"mem\\server\\requirements.lock.txt"' in setup
    assert '"scripts\\configure_local.py"' in setup


def test_bundled_memory_package_build_does_not_require_removed_markdown() -> None:
    pyproject = (ROOT / "mem" / "pyproject.toml").read_text(encoding="utf-8")

    assert 'readme = "README.md"' not in pyproject
    assert 'readme = { text = ' in pyproject


def test_readme_explains_rebuild_and_non_recoverable_state() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for required in (
        "Setup-AICustomerService.ps1",
        "GitHub 没有上传什么",
        "acs.db",
        "Chrome Profile",
        "API Key",
        "不会恢复旧客户与会话数据",
    ):
        assert required in readme


def test_password_encryption_cli_never_echoes_plaintext(monkeypatch, capsys) -> None:
    plaintext = "local-only-password"
    monkeypatch.setattr(sys, "argv", ["widget.secret"])
    monkeypatch.setattr(widget_secret, "getpass", lambda _prompt: plaintext)
    monkeypatch.setattr(widget_secret, "encrypt", lambda _value: "encrypted-value")

    widget_secret.main()

    output = capsys.readouterr()
    assert plaintext not in output.out
    assert plaintext not in output.err
    assert "encrypted-value" in output.out


def test_password_encryption_cli_rejects_password_argument(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["widget.secret", "must-not-be-used"])

    with pytest.raises(SystemExit) as error:
        widget_secret.main()

    assert error.value.code == 2
    captured = capsys.readouterr()
    assert "must-not-be-used" not in captured.out
    assert "must-not-be-used" not in captured.err
