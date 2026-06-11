"""Where PartPilot keeps its runtime data — shared by the web app and the CLI."""

from __future__ import annotations

import os
from pathlib import Path

from ...config import PROJECT_ROOT


def data_dir() -> Path:
    env = os.getenv("PARTPILOT_DATA_DIR")
    return Path(env) if env else PROJECT_ROOT / "data"


def db_path() -> Path:
    env = os.getenv("PARTPILOT_DB")
    return Path(env) if env else data_dir() / "partpilot.db"
