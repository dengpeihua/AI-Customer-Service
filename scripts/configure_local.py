"""Prepare ignored local configuration and a fresh database after a source clone."""
from __future__ import annotations

import argparse
import base64
from getpass import getpass
import os
from pathlib import Path
import secrets
import shutil
import sys


ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDERS = {"", "待填写", "change-me", "your-api-key"}
LOCAL_TEMPLATES = {
    ".env": ".env.example",
    "mem/.env": "mem/.env.example",
    "widget_config.yaml": "widget_config.example.yaml",
    "douyin_accounts.yaml": "douyin_accounts.example.yaml",
}


def _configured(value: str | None) -> bool:
    return bool(value and value.strip() not in PLACEHOLDERS)


def _read_env(path: Path) -> tuple[list[str], dict[str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return lines, values


def _set_env(path: Path, key: str, value: str) -> None:
    lines, _ = _read_env(path)
    prefix = f"{key}="
    replacement = f"{key}={value}"
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = replacement
            break
    else:
        lines.append(replacement)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ensure_local_files(root: Path) -> list[Path]:
    created: list[Path] = []
    for destination, template in LOCAL_TEMPLATES.items():
        destination_path = root / destination
        if destination_path.exists():
            continue
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / template, destination_path)
        created.append(destination_path)
    (root / "mem" / "data").mkdir(parents=True, exist_ok=True)
    return created


def generate_application_secrets(env_path: Path) -> list[str]:
    _, values = _read_env(env_path)
    generated: list[str] = []
    replacements = {
        "JWT_SECRET": secrets.token_urlsafe(48),
        "BOOTSTRAP_TOKEN": secrets.token_urlsafe(48),
        "FIELD_ENC_KEY": base64.b64encode(os.urandom(32)).decode("ascii"),
    }
    for key, value in replacements.items():
        if _configured(values.get(key)):
            continue
        _set_env(env_path, key, value)
        generated.append(key)
    return generated


def _prompt_secret(name: str, *, non_interactive: bool) -> str:
    explicit = os.getenv(f"ACS_SETUP_{name}", "").strip()
    if explicit:
        return explicit
    if non_interactive:
        return ""
    return getpass(f"{name}（隐藏输入；留空表示稍后填写）: ").strip()


def configure_provider_keys(env_path: Path, *, non_interactive: bool) -> list[str]:
    _, values = _read_env(env_path)
    updated: list[str] = []
    for name in ("MINIMAX_API_KEY", "DEEPSEEK_API_KEY"):
        if _configured(values.get(name)):
            continue
        value = _prompt_secret(name, non_interactive=non_interactive)
        if value:
            _set_env(env_path, name, value)
            updated.append(name)
    return updated


def apply_migrations(root: Path) -> None:
    from alembic import command
    from alembic.config import Config

    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    command.upgrade(config, "head")


def _admin_credentials(*, non_interactive: bool) -> tuple[str, str] | None:
    login = os.getenv("ACS_SETUP_ADMIN_LOGIN", "").strip()
    password = os.getenv("ACS_SETUP_ADMIN_PASSWORD", "")
    if non_interactive:
        return (login or "admin", password) if password else None

    login = input("新数据库管理员账号 [admin]: ").strip() or "admin"
    while True:
        password = getpass("新数据库管理员密码（隐藏输入，至少 8 个字符）: ")
        if not 8 <= len(password) <= 72:
            print("密码长度必须为 8–72 个字符。")
            continue
        if password != getpass("再次输入管理员密码: "):
            print("两次输入不一致。")
            continue
        return login, password


def _write_local_login(root: Path, tenant_id: int, login: str, password: str) -> None:
    import yaml
    from widget.secret import encrypt

    encrypted = encrypt(password)
    widget_path = root / "widget_config.yaml"
    widget = yaml.safe_load(widget_path.read_text(encoding="utf-8")) or {}
    widget.update({
        "tenant_id": tenant_id,
        "login": login,
        "password": "",
        "password_enc": encrypted,
        "auto_send": False,
    })
    widget_path.write_text(
        yaml.safe_dump(widget, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    accounts_path = root / "douyin_accounts.yaml"
    account_config = yaml.safe_load(accounts_path.read_text(encoding="utf-8")) or {}
    for account in account_config.get("accounts", []):
        account.update({
            "tenant_id": tenant_id,
            "login": login,
            "password_enc": encrypted,
            "enabled": False,
            "send_enabled": False,
        })
        account.pop("password", None)
    accounts_path.write_text(
        yaml.safe_dump(account_config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def bootstrap_database(root: Path, *, non_interactive: bool) -> bool:
    from sqlalchemy import func, select

    from app.crud.tenant import create_tenant
    from app.crud.user import create_user
    from app.db import SessionLocal
    from app.models.user import User

    with SessionLocal() as session:
        user_count = int(session.scalar(select(func.count(User.id))) or 0)
        if user_count:
            return True

        credentials = _admin_credentials(non_interactive=non_interactive)
        if credentials is None:
            return False
        login, password = credentials
        tenant = create_tenant(session, name="AI Customer Service")
        create_user(session, tenant_id=tenant.id, login=login, password=password, role="admin")
        _write_local_login(root, tenant.id, login, password)
        return True


def configuration_problems(root: Path) -> list[str]:
    import yaml

    _, values = _read_env(root / ".env")
    provider = values.get("LLM_PROVIDER", "minimax").strip().lower()
    required = {
        "minimax": ("MINIMAX_API_KEY",),
        "glm": ("GLM_API_KEY",),
        "deepseek": ("DEEPSEEK_API_KEY", "MINIMAX_API_KEY"),
    }.get(provider, ())
    problems = [name for name in required if not _configured(values.get(name))]
    if not required:
        problems.append("LLM_PROVIDER")

    from sqlalchemy import func, select

    from app.db import SessionLocal
    from app.models.user import User

    with SessionLocal() as session:
        if int(session.scalar(select(func.count(User.id))) or 0) == 0:
            problems.append("ADMIN_ACCOUNT")
    widget = yaml.safe_load((root / "widget_config.yaml").read_text(encoding="utf-8")) or {}
    has_widget_identity = bool(widget.get("tenant_id") and widget.get("login"))
    has_widget_password = bool(widget.get("password_enc") or widget.get("password"))
    if not has_widget_identity or not has_widget_password:
        problems.append("WIDGET_CREDENTIALS")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    if not args.check_only:
        created = ensure_local_files(ROOT)
        generated = generate_application_secrets(ROOT / ".env")
        configured = configure_provider_keys(
            ROOT / ".env",
            non_interactive=args.non_interactive,
        )
        print(f"local_files_created={len(created)}")
        print(f"application_secrets_generated={len(generated)}")
        print(f"provider_keys_configured={len(configured)}")

    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=True)
    apply_migrations(ROOT)
    if not args.check_only:
        bootstrap_database(ROOT, non_interactive=args.non_interactive)

    problems = configuration_problems(ROOT)
    if problems:
        print("configuration=INCOMPLETE missing=" + ",".join(problems))
    else:
        print("configuration=READY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
