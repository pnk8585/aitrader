"""Central logging for aitrader.

Sinks under AITRADER_STATE_DIR/logs (or ./logs):

  scheduler.log / cron.log / jobs/*.log  — ops (existing)
  aitrader.log / named *.log             — app loggers
  llm.jsonl                              — one JSON object per LLM call

Log level (default INFO) from app_settings ``logging.level`` (or env LOG_LEVEL).
uvicorn.access HTTP lines are forced to DEBUG so they stay quiet at INFO
(same behaviour as bettips-ai).

Env:
  AITRADER_STATE_DIR  — durable state root (Docker: /state)
  LOG_DIR             — override log directory
  LOG_LLM_PROMPTS     — "0"/"false"/"no" disables llm.jsonl (default: on)
  LOG_LEVEL           — fallback if DB setting missing (DEBUG|INFO|WARNING|ERROR)
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

_LEVEL_NAMES = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "WARN": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

_CONFIGURED = False
_llm_lock = threading.Lock()
_llm_path: str | None = None
_log_llm_prompts = True
_current_level = logging.INFO
_current_level_name = "INFO"
# Track handlers we created so apply_log_level can retarget them
_managed_handlers: list[logging.Handler] = []
_named_loggers: set[str] = set()


class _AccessAsDebugFilter(logging.Filter):
    """Rewrite uvicorn.access records to DEBUG so they only show when level is DEBUG."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.levelno = logging.DEBUG
        record.levelname = "DEBUG"
        return True


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


def _parse_level(level_name: str | None) -> tuple[int, str]:
    name = (level_name or "INFO").strip().upper()
    if name not in _LEVEL_NAMES:
        name = "INFO"
    return _LEVEL_NAMES[name], name


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


def _silence_or_enable_access(app_level: int) -> None:
    """uvicorn.access handlers are often NOTSET (accept everything).

    At INFO+: access logger WARNING so INFO access lines never print.
    At DEBUG: show access, rewritten to DEBUG via filter.
    """
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _AccessAsDebugFilter) for f in access.filters):
        access.addFilter(_AccessAsDebugFilter())

    if app_level <= logging.DEBUG:
        access.setLevel(logging.DEBUG)
        for h in list(access.handlers):
            h.setLevel(logging.DEBUG)
        access.propagate = False
    else:
        access.setLevel(logging.WARNING)
        for h in list(access.handlers):
            h.setLevel(logging.WARNING)
        access.propagate = False


def apply_log_level(level_name: str | None = None) -> str:
    """Set app + console log level. Access logs only when level is DEBUG.

    At INFO (default): HTTP access lines are hidden.
    At DEBUG: access lines appear as DEBUG.
    """
    global _current_level, _current_level_name
    level, name = _parse_level(level_name)
    _current_level = level
    _current_level_name = name

    # Named aitrader loggers (scheduler, cron, aitrader, …)
    for n in list(_named_loggers) + ["aitrader", "aitrader.llm"]:
        logging.getLogger(n).setLevel(level)

    for h in _managed_handlers:
        h.setLevel(level)

    # uvicorn.error: lifecycle visible at INFO+
    err = logging.getLogger("uvicorn.error")
    err.setLevel(logging.INFO if level > logging.DEBUG else logging.DEBUG)

    _silence_or_enable_access(level)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if level <= logging.DEBUG else logging.INFO)
    for h in root.handlers:
        if h not in _managed_handlers:
            h.setLevel(logging.DEBUG if level <= logging.DEBUG else logging.INFO)

    for lib in ("httpx", "httpcore", "httpcore.connection", "httpcore.http11", "urllib3"):
        logging.getLogger(lib).setLevel(
            logging.DEBUG if level <= logging.DEBUG else logging.WARNING
        )

    return name


def apply_log_level_from_settings() -> str:
    """Read logging.level from DB (then env LOG_LEVEL), default INFO."""
    level_name = None
    try:
        from app.settings import get_setting
        level_name = get_setting("logging.level")
    except Exception:
        pass
    if level_name:
        level_name = str(level_name).strip()
    if not level_name:
        level_name = (os.getenv("LOG_LEVEL") or "INFO").strip()
    applied = apply_log_level(level_name)
    logging.getLogger("aitrader").info(
        "Log level → %s (HTTP access hidden unless DEBUG)", applied
    )
    return applied


def setup_logging(name: str = "aitrader", *, level: int | None = None) -> logging.Logger:
    """Named logger: StreamHandler + file under logs_dir/{name}.log.

    Level defaults to current app level (from apply_log_level / settings).
    """
    global _CONFIGURED
    log = logging.getLogger(name)
    _named_loggers.add(name)

    use_level = level if level is not None else _current_level

    if log.handlers:
        log.setLevel(use_level)
        for h in log.handlers:
            h.setLevel(use_level)
        _ensure_llm_path()
        return log

    log.setLevel(use_level)
    log.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.setLevel(use_level)
    log.addHandler(sh)
    _managed_handlers.append(sh)

    try:
        d = logs_dir()
        d.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(d / f"{name}.log", encoding="utf-8")
        fh.setFormatter(fmt)
        fh.setLevel(use_level)
        log.addHandler(fh)
        _managed_handlers.append(fh)
    except OSError as e:
        log.warning("Could not open file log under %s: %s", logs_dir(), e)

    # Quiet access + libs once (first setup_logging call)
    apply_log_level(logging.getLevelName(use_level))

    _ensure_llm_path()
    return log


def configure_logging() -> str | None:
    """Bootstrap logging (access→DEBUG, app at INFO/env) before DB is ready.

    Returns log directory path if available.
    """
    apply_log_level(os.getenv("LOG_LEVEL") or "INFO")
    setup_logging("aitrader")
    try:
        d = logs_dir()
        d.mkdir(parents=True, exist_ok=True)
        return str(d)
    except OSError:
        return None


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
