"""Start the checked-in Mem0 OSS server against the complete LoCoMo index."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
MEM0_ROOT = ROOT / "mem"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8888)
    args = parser.parse_args()

    # Force both code and data to this delivery, even if the bundled venv was once used
    # with an editable install from another checkout on the same Windows account.
    sys.path.insert(0, str(MEM0_ROOT / "server"))
    sys.path.insert(0, str(MEM0_ROOT))
    os.environ["PYTHONPATH"] = str(MEM0_ROOT)
    os.environ["MEM0_DATA_DIR"] = str(MEM0_ROOT / "data")
    os.environ["MEM0_TELEMETRY"] = "false"
    os.chdir(MEM0_ROOT / "server")

    import uvicorn

    uvicorn.run("main:app", host=args.host, port=args.port, workers=1)


if __name__ == "__main__":
    main()
