"""Cron management — read/write the shared registry JSON.

ponytail: run-now = nudge next_run, host cron does the spawn.
ponytail: safe writes via aitrader_registry._save (re-read + deep-merge).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from app.state import _STATE_DIR
# Use the canonical _save for atomic, non-clobbering writes
from aitrader_registry import _save as _save_registry

ATHENS = timezone(timedelta(hours=3))
VALID_MODES = frozenset({"live", "paper", "paused"})


def _find_registry() -> Path | None:
    for name in ("registry.json", "aitrader_orchestrator.json"):
        path = _STATE_DIR / name
        if path.exists():
            return path
    return None


def _read_registry() -> dict:
    path = _find_registry()
    if path:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"scripts": {}}


def list_scripts() -> dict:
    """Return {name: {status, mode, interval, last_run, next_run, path, note}}."""
    return _read_registry().get("scripts", {})


def set_mode(name: str, mode: str) -> None:
    if mode not in VALID_MODES:
        raise ValueError(
            f"Invalid mode '{mode}'. Must be one of: {', '.join(sorted(VALID_MODES))}"
        )
    reg = _read_registry()
    if name not in reg.get("scripts", {}):
        raise KeyError(f"Script '{name}' not found in registry")
    _save_registry({"scripts": {name: {"mode": mode}}})
    print(f"[cron] mode change: {name} -> {mode}", file=sys.stderr)


def run_now(name: str) -> None:
    reg = _read_registry()
    if name not in reg.get("scripts", {}):
        raise KeyError(f"Script '{name}' not found in registry")
    now = datetime.now(ATHENS).isoformat()
    _save_registry({"scripts": {name: {"next_run": now, "status": "running"}}})
    print(f"[cron] run-now: {name}", file=sys.stderr)


def pause(name: str) -> None:
    reg = _read_registry()
    if name not in reg.get("scripts", {}):
        raise KeyError(f"Script '{name}' not found in registry")
    _save_registry({"scripts": {name: {"status": "paused"}}})
    print(f"[cron] pause: {name}", file=sys.stderr)


def resume(name: str) -> None:
    reg = _read_registry()
    if name not in reg.get("scripts", {}):
        raise KeyError(f"Script '{name}' not found in registry")
    _save_registry({"scripts": {name: {"status": "running"}}})
    print(f"[cron] resume: {name}", file=sys.stderr)
