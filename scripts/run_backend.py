"""用项目 ``.env`` 启动后端，避免父进程环境变量覆盖本地配置。"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--print-runtime", action="store_true")
    args = parser.parse_args()

    # 这是 Windows 本地交付入口，项目 .env 是用户明确编辑的权威配置。
    # override=True 可避免 Explorer/UAC 继承的旧用户环境变量悄悄盖掉新密钥。
    load_dotenv(ROOT / ".env", override=True)
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))

    if args.print_runtime:
        from app.llm import runtime_summary

        print(runtime_summary())
        return

    # 交付入口必须让代码与本地数据库结构保持同一版本。升级只追加/变更 schema，Alembic
    # 自己记录版本；这里不清空、不重建任何业务表。失败则拒绝启动，避免服务表面健康但新页面不可用。
    from alembic import command
    from alembic.config import Config

    alembic_cfg = Config(str(ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(alembic_cfg, "head")

    import uvicorn

    uvicorn.run("app.main:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
