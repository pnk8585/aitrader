#!/usr/bin/env python3
"""AITrader Orchestrator — one cron to rule them all. Every 1'.

Reads registry JSON, spawns trading scripts when their next_run time arrives.
Scripts update their own status/next_run/mode in the registry.

Pattern copied from superbet_orchestrator.py — same atomic merge, same spawn logic.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import aitrader_registry as orch  # noqa: E402

ATHENS = timezone(timedelta(hours=3))
REGISTRY_PATH = ROOT / "aitrader_orchestrator.json"
GRACE_SECONDS = 30
STUCK_ONGOING_MINUTES = 15  # trading scripts can take longer than superbet


def load_registry() -> dict:
    return orch._load()


def merge_save(partial: dict) -> None:
    orch._save(partial)


def should_run(script: dict, now: datetime) -> bool:
    status = script.get("status", "paused")
    if status not in ("running",):
        return False

    # Also skip paused scripts (mode check)
    if script.get("mode") == "paused":
        return False

    next_run = script.get("next_run")
    if not next_run:
        return True  # stale → run now

    try:
        nt = datetime.fromisoformat(next_run)
        delta = (now - nt).total_seconds()
        return delta >= -GRACE_SECONDS
    except (TypeError, ValueError):
        return True


def spawn_script(name: str, path: str, mode: str, reg: dict) -> None:
    script_path = ROOT / path
    if not script_path.is_file():
        _append_log(reg, name, f"script_not_found: {script_path}")
        return

    # Mark ongoing to prevent double-spawn
    reg["scripts"][name]["status"] = "ongoing"
    merge_save({"scripts": {name: {"status": "ongoing"}}})

    env = os.environ.copy()
    env["AITRADER_MODE"] = mode

    try:
        subprocess.Popen(
            [sys.executable, str(script_path)],
            cwd=str(ROOT),
            env=env,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _append_log(reg, name, f"spawned ({mode})")
    except Exception as e:
        reg["scripts"][name]["status"] = "running"
        merge_save({"scripts": {name: {"status": "running"}}})
        _append_log(reg, name, f"spawn_failed: {e}")


def main() -> int:
    now = datetime.now(ATHENS)
    reg = load_registry()
    scripts = reg.get("scripts", {})
    spawned = 0

    # Guard: reset stuck "ongoing" scripts
    for name, script in scripts.items():
        if script.get("status") == "ongoing":
            last = script.get("last_run") or script.get("next_run")
            if last:
                try:
                    lt = datetime.fromisoformat(last)
                    stuck_min = (now - lt).total_seconds() / 60
                    if stuck_min > STUCK_ONGOING_MINUTES:
                        script["status"] = "running"
                        script["next_run"] = now.isoformat()
                        merge_save({"scripts": {name: {
                            "status": "running",
                            "next_run": now.isoformat()
                        }}})
                        _append_log(reg, name, f"unstuck_ongoing_after_{int(stuck_min)}m")
                except (TypeError, ValueError):
                    script["status"] = "running"
                    script["next_run"] = now.isoformat()
                    merge_save({"scripts": {name: {
                        "status": "running",
                        "next_run": now.isoformat()
                    }}})

    for name, script in scripts.items():
        if not script.get("path"):
            continue
        if should_run(script, now):
            mode = script.get("mode", "live")
            spawn_script(name, script["path"], mode, reg)
            spawned += 1

    if spawned:
        pass  # already logged per-script

    # Persist orchestrator-owned fields
    merge_save({"orchestrator": {
        "last_run": now.isoformat(),
    }})
    return 0


def _append_log(reg: dict, name: str, msg: str) -> None:
    """Append to in-memory log (persisted on next merge). Simple print for now."""
    ts = datetime.now(ATHENS).strftime("%H:%M:%S")
    print(f"[{ts}] {name}: {msg}")


if __name__ == "__main__":
    raise SystemExit(main())
