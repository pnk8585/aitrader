"""Resolve AITRADER_STATE_DIR and expose path helpers."""

from __future__ import annotations

import os
from pathlib import Path

_STATE_DIR = Path(os.environ.get("AITRADER_STATE_DIR", "/home/pank/projects/aitrader"))


def registry_path() -> Path:
    return _STATE_DIR / "registry.json"


def env_path() -> Path:
    return _STATE_DIR / ".env"


def logs_dir() -> Path:
    return _STATE_DIR / "logs"
