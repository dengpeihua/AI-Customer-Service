"""预检或导入 dataset/*.json 到配置的 acs.db 知识库。"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default="dataset", help="JSON 文件或目录，默认 dataset")
    parser.add_argument("--tenant-id", type=int, help="目标租户；省略时读取 widget_config.yaml")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正写入数据库；不加时只做 JSON 预检",
    )
    parser.add_argument(
        "--allow-duplicate-titles",
        action="store_true",
        help="允许重复标题（默认跳过同租户已有标题）",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)

    from app.kb.dataset import import_dataset, load_dataset

    documents = load_dataset(args.dataset)
    print(f"dataset_check=ok files={len({d.source_file for d in documents})} documents={len(documents)}")
    for document in documents:
        print(
            f"document title={document.title!r} chars={len(document.content)} "
            f"source={document.source_file.name!r}"
        )
    if not args.apply:
        print("dry_run=true database_unchanged=true; add --apply to import")
        return

    tenant_id = args.tenant_id
    if tenant_id is None:
        from widget.config import load_config
        tenant_id = load_config(PROJECT_ROOT / "widget_config.yaml").tenant_id
    if tenant_id is None or tenant_id <= 0:
        raise SystemExit("tenant id 必须大于 0；请传 --tenant-id")

    from app.db import SessionLocal
    from app.llm import get_llm
    from app.models.tenant import Tenant

    with SessionLocal() as db:
        if db.get(Tenant, tenant_id) is None:
            raise SystemExit(f"tenant id {tenant_id} 不存在，未写入数据库")
        result = import_dataset(
            db,
            get_llm(),
            tenant_id=tenant_id,
            documents=documents,
            skip_existing_titles=not args.allow_duplicate_titles,
        )
    print(
        f"dataset_import=ok discovered={result.discovered} "
        f"imported={result.imported} skipped={result.skipped}"
    )


if __name__ == "__main__":
    main()
