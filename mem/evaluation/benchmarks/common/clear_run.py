"""Safely remove one local benchmark run and its Mem0 users."""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

import requests

from benchmarks.common.paths import RESULTS_DIR


VALID_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _find_user_ids(run_dir: Path) -> list[str]:
    user_ids: set[str] = set()
    for pattern in ("_ingestion_*.json", "_progress_*.json"):
        for path in run_dir.glob(pattern):
            data = _load_json(path)
            if data and data.get("user_id"):
                user_ids.add(str(data["user_id"]))
    return sorted(user_ids)


def _find_result_files(benchmark: str, project_name: str) -> list[Path]:
    result_dir = RESULTS_DIR / benchmark
    matches = []
    for path in result_dir.glob(f"{benchmark}_results_*.json"):
        data = _load_json(path)
        metadata = data.get("metadata", {}) if data else {}
        if metadata.get("benchmark") == benchmark and metadata.get("project_name") == project_name:
            matches.append(path)
    return sorted(matches)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", required=True, choices=["locomo", "longmemeval", "beam"])
    parser.add_argument("--project-name", required=True)
    parser.add_argument("--mem0-host", default=None, help="Defaults to MEM0_HOST from the repository .env")
    parser.add_argument("--files-only", action="store_true", help="Do not call the local Mem0 service")
    parser.add_argument("--yes", action="store_true", help="Execute deletion; otherwise only print the plan")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not VALID_NAME.fullmatch(args.project_name):
        raise SystemExit("project name may contain only letters, digits, dot, underscore, and hyphen")

    run_root = (RESULTS_DIR / args.benchmark).resolve()
    run_dir = (run_root / f"predicted_{args.project_name}").resolve()
    if run_dir.parent != run_root:
        raise SystemExit("refusing to operate outside the benchmark result directory")

    user_ids = _find_user_ids(run_dir) if run_dir.is_dir() else []
    result_files = _find_result_files(args.benchmark, args.project_name)

    print(f"Run directory: {run_dir if run_dir.exists() else '(not found)'}")
    print(f"Mem0 users: {', '.join(user_ids) if user_ids else '(none found)'}")
    print("Result files:")
    for path in result_files:
        print(f"  {path}")

    if not args.yes:
        print("Dry run only. Add --yes to execute.")
        return

    if user_ids and not args.files_only:
        import os

        host = (args.mem0_host or os.getenv("MEM0_HOST", "http://127.0.0.1:8888")).rstrip("/")
        for user_id in user_ids:
            response = requests.delete(
                f"{host}/memories",
                params={"user_id": user_id},
                timeout=60,
            )
            response.raise_for_status()
            print(f"Deleted local memories for {user_id}")

    if run_dir.is_dir():
        shutil.rmtree(run_dir)
        print(f"Deleted {run_dir}")
    for path in result_files:
        path.unlink()
        print(f"Deleted {path}")


if __name__ == "__main__":
    main()
