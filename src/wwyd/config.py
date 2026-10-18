# SPDX-License-Identifier: AGPL-3.0-or-later
"""Runtime configuration (M1: data dir only)."""

from __future__ import annotations

import os
from pathlib import Path

DB_FILENAME = "wwyd.db"


def data_dir() -> Path:
    """Return the data directory (``WWYD_DATA_DIR`` or ``~/.wwyd``), creating it if needed."""
    raw = os.environ.get("WWYD_DATA_DIR")
    path = Path(raw).expanduser() if raw else Path.home() / ".wwyd"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return data_dir() / DB_FILENAME
