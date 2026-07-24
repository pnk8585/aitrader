"""Central logging for aitrader.

Sinks under AITRADER_STATE_DIR/logs (or ./logs):

  scheduler.log / cron.log / jobs/*.log  — ops (existing)
  llm.jsonl                             — one JSON object per LLM call
                                          (prompt + response + verdict + latency)

Env:
  AITRADER_STATE_DIR  — durable state root (Docker: /state)
  LOG_DIR             — override log directory
  LOG_LLM_PROMPTS     — "0"/"false"/"no" disables llm.jsonl (default: on)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_CONFIGURED = False
_llm_lock = threading.Lock()
_llm_path: str | None = None
_log_llm_prompts = True


def _env_truthy(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def logs_dir() -> Path:
    """Prefer LOG_DIR, then AITRADER_STATE_DIR/logs, else repo logs/."""
    env_dir = os.getenv("LOG_DIR")
    if env_dir:
        return Path(env_dir)
    state = os.environ.get("AITRADER_STATE_DIR")
    if state:
        return Path(state) / "logs"
    return Path(__file__).resolve().parent.parent / "logs"


def _ensure_llm_path() -> None:
    """Resolve llm.jsonl path once (lazy)."""
    global _llm_path, _log_llm_prompts, _CONFIGURED
    if _llm_path is not None or (_CONFIGURED and not _log_llm_prompts):
        return
    _log_llm_prompts = _env_truthy("LOG_LLM_PROMPTS", default=True)
    if not _log_llm_prompts:
        _CONFIGURED = True
        return
    try:
        d = logs_dir()
        d.mkdir(parents=True, exist_ok=True)
        _llm_path = str(d / "llm.jsonl")
    except OSError:
        _llm_path = None
    _CONFIGURED = True


def setup_logging(name: str = "aitrader", *, level: int = logging.INFO) -> logging.Logger:
    """Named logger: StreamHandler + file under logs_dir/{name}.log."""
    global _CONFIGURED
    log = logging.getLogger(name)
    if log.handlers:
        _ensure_llm_path()
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

    _ensure_llm_path()
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


def log_llm_call(
    *,
    kind: str,
    model: str,
    system_prompt: str = "",
    user_prompt: str = "",
    response: str | None = None,
    status: str = "ok",
    error: str | None = None,
    latency_ms: float | None = None,
    symbol: str | None = None,
    strategy: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append one structured LLM audit record to llm.jsonl (if enabled).

    Mirrors bettips-ai: full prompts + response for later accuracy analysis.
    """
    _ensure_llm_path()
    if not _log_llm_prompts or not _llm_path:
        return

    record: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "model": model,
        "symbol": symbol,
        "strategy": strategy,
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "response": response,
        "latency_ms": round(latency_ms, 1) if latency_ms is not None else None,
        "status": status,
        "error": error,
    }
    if extra:
        record["extra"] = extra

    line = json.dumps(record, ensure_ascii=False, default=str)
    try:
        with _llm_lock:
            with open(_llm_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError as e:
        logging.getLogger("aitrader.llm").warning("Failed to write llm.jsonl: %s", e)
