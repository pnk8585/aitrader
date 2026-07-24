"""Shared logging: stdout (docker logs) + durable files under state/logs."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

_CONFIGURED = False


def logs_dir() -> Path:
    """Prefer AITRADER_STATE_DIR/logs; else repo logs/."""
    state = os.environ.get("AITRADER_STATE_DIR")
    if state:
        return Path(state) / "logs"
    return Path(__file__).resolve().parent.parent / "logs"


def setup_logging(name: str = "aitrader", *, level: int = logging.INFO) -> logging.Logger:
    """Idempotent root+named logger: StreamHandler + rotating-ish daily file.

    File: {logs_dir}/{name}.log  (append; docker json-file still captures stdout)
    """
    global _CONFIGURED
    log = logging.getLogger(name)
    if _CONFIGURED and log.handlers:
        return log

    log.setLevel(level)
    log.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.setLevel(level)
    log.addHandler(sh)

    try:
        d = logs_dir()
        d.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(d / f"{name}.log", encoding="utf-8")
        fh.setFormatter(fmt)
        fh.setLevel(level)
        log.addHandler(fh)
    except OSError as e:
        log.warning("Could not open file log under %s: %s", logs_dir(), e)

    _CONFIGURED = True
    return log


def append_job_log(job_name: str, header: str, body: str) -> Path | None:
    """Append one job run's full stdout/stderr to logs/jobs/{job}.log."""
    try:
        d = logs_dir() / "jobs"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{job_name}.log"
        with path.open("a", encoding="utf-8") as f:
            f.write(header.rstrip() + "\n")
            if body:
                f.write(body.rstrip() + "\n")
            f.write("\n")
        return path
    except OSError:
        return None
