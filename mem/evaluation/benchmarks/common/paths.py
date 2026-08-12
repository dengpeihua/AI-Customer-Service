"""Stable paths for the benchmarks embedded in the Mem0 repository."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(PROJECT_ROOT / ".env", override=True)

DATA_ROOT = Path(os.getenv("MEM0_DATA_DIR", PROJECT_ROOT / "data")).resolve()
DATASETS_DIR = DATA_ROOT / "datasets"
RESULTS_DIR = DATA_ROOT / "results"
LOGS_DIR = DATA_ROOT / "logs"

