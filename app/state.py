"""Resolve AITRADER_STATE_DIR and expose path helpers."""

from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_ROOT = Path(__file__).resolve().parent.parent
_STATE_DIR = Path(os.environ.get("AITRADER_STATE_DIR") or str(_DEFAULT_ROOT))


def state_dir() -> Path:
    return _STATE_DIR


def env_path() -> Path:
    p = _STATE_DIR / ".env"
    if p.is_file():
        return p
    return _DEFAULT_ROOT / ".env"


def logs_dir() -> Path:
    return _STATE_DIR / "logs"
