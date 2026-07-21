#!/usr/bin/env python3
"""Registry helpers — import in scripts to update orchestrator state.

Same atomic merge pattern as superbet's orchestrator_registry.py.
"""
from __future__ import annotations

import json
import os as _os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REGISTRY_PATH = ROOT / "aitrader_orchestrator.json"

ATHENS = timezone(timedelta(hours=3))


def _load() -> dict:
    try:
        return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"scripts": {}}


def _save(partial: dict) -> None:
    """Atomically merge updates — never clobber concurrent writes."""
    current = {}
    if REGISTRY_PATH.is_file():
        try:
            current = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        except Exception:
            current = {}

    # Deep merge: partial overwrites only the keys it touches
    for section in ("scripts", "orchestrator"):
        if section in partial:
            if section not in current:
                current[section] = {}
            for k, v in partial[section].items():
                if isinstance(v, dict) and isinstance(current[section].get(k), dict):
                    current[section][k] = {**current[section][k], **v}
                else:
                    current[section][k] = v

    # Atomic write via temp file + rename
    fd, tmp = tempfile.mkstemp(
        suffix=".json", prefix="orch_", dir=str(REGISTRY_PATH.parent)
    )
    try:
        with _os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=2)
        _os.replace(tmp, str(REGISTRY_PATH))
    except Exception:
        try:
            _os.unlink(tmp)
        except Exception:
            pass


def update(name: str, *, status: str | None = None,
           next_run: str | None = None, note: str = "") -> None:
    reg = _load()
    s = reg.setdefault("scripts", {}).setdefault(name, {})
    if status is not None:
        s["status"] = status
    if next_run is not None:
        s["next_run"] = next_run
    if "last_run" not in s or status in ("running", "ongoing"):
        s["last_run"] = datetime.now(ATHENS).isoformat()
    if note:
        s["note"] = note
    _save({"scripts": {name: s}})


def mark_started(name: str) -> None:
    update(name, status="ongoing",
           note=f"started {datetime.now(ATHENS).strftime('%H:%M:%S')}")


def mark_done(name: str, next_run: str, *,
              status: str = "running", note: str = "") -> None:
    update(name, status=status, next_run=next_run, note=note)


def pause(name: str, reason: str = "") -> None:
    update(name, status="paused", note=reason)


def resume(name: str, next_run: str | None = None) -> None:
    if _load().get("scripts", {}).get(name, {}).get("status") == "ongoing":
        if next_run is not None:
            update(name, next_run=next_run)
        return
    update(name, status="running", next_run=next_run)


def set_next(name: str, minutes: int, reason: str = "") -> str:
    """Convenience: set next_run = now + minutes. Returns ISO string."""
    now = datetime.now(ATHENS) + timedelta(minutes=minutes)
    iso = now.isoformat()
    update(name, status="running", next_run=iso, note=reason)
    return iso


def is_paused(name: str) -> bool:
    return _load().get("scripts", {}).get(name, {}).get("status") == "paused"


def get_mode(name: str) -> str:
    """Return the script's mode: live, paper, or paused."""
    return _load().get("scripts", {}).get(name, {}).get("mode", "live")


def get_status(name: str) -> str:
    return _load().get("scripts", {}).get(name, {}).get("status", "live")
