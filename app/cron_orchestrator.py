"""DB-driven cron orchestrator for aitrader.

Single tick() called every minute by host cron. Each job is a subprocess,
not a coroutine — trading scripts are standalone Python files.

Modeled on bettips-ai's cron_orchestrator.py, adapted for psycopg2 + subprocess.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.notify import send_telegram
from app.logging_setup import append_job_log, setup_logging
from app.market_schedule import alpaca_market_status, deferred_next_run

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

log = setup_logging("cron")

# name → (script_path, interval_seconds, default_mode)
JOB_REGISTRY: dict[str, tuple[str, int, str]] = {
    "kraken-pullback":    ("traders/crypto_trades/kraken_pullback.py", 300,  "live"),
    "kraken-grid":        ("traders/crypto_trades/kraken_grid.py",     300,  "paper"),
    "kraken-momentum":    ("traders/crypto_trades/kraken_momentum.py", 300,  "paper"),
    "kraken-high-risk":   ("traders/crypto_trades/kraken_high_risk.py", 300,  "paper"),
    "position-monitor":   ("position_monitor.py",                       7200, "live"),
    "alpaca-stocks":      ("traders/trades/alpaca_stocks.py",           300,  "paper"),
    "end-of-day-review":  ("traders/eod_review.py",                    86400, "live"),
    "health-check":       ("scripts/health_check.py",                 86400, "live"),
    "hourly-report":      ("scripts/hourly_report.py",                3600,  "live"),
    "db-cleanup":         ("scripts/db_cleanup.py",                   86400, "live"),
    "weekly-rethink":     ("traders/weekly_rethink.py",           7 * 86400, "live"),
}

_RUNNING_STALE = timedelta(hours=3)

# Jobs that only notify on real trade signals (not every run)
_TRADE_SIGNAL_JOBS = frozenset({
    "kraken-pullback", "kraken-momentum", "kraken-grid", "kraken-high-risk",
    "alpaca-stocks", "position-monitor", "hourly-report",
})
_TRADE_SIGNAL_KEYWORDS = ("BUY", "SELL", "🛒", "🔄", "entry", "exit",
                           "trade placed", "order filled", "bought", "sold",
                           "opening", "closing", "⚠️", "PENDING_AI_REVIEW",
                           "HARD STOP", "Sold ", "❌")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _next_athens_hour(hour: int = 5) -> datetime:
    """Next wall-clock ``hour``:00 in Europe/Athens, as UTC-aware datetime."""
    from zoneinfo import ZoneInfo
    ath = ZoneInfo("Europe/Athens")
    now_ath = datetime.now(timezone.utc).astimezone(ath)
    target = now_ath.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now_ath:
        target += timedelta(days=1)
    return target.astimezone(timezone.utc)


# Jobs that should fire at a fixed Athens hour (not pure interval-from-now)
_FIXED_ATHENS_HOUR: dict[str, int] = {
    "db-cleanup": 5,  # 05:00 Athens daily
    "health-check": 21,  # 21:00 Athens — after end-of-day-review (20:00)
}

# Jobs that should fire on a fixed Athens weekday (1=Mon..7=Sun) + hour
_FIXED_ATHENS_WEEKDAY: dict[str, tuple[int, int]] = {
    "weekly-rethink": (7, 9),  # Sunday 09:00 Athens
}


def _next_athens_weekday_hour(weekday: int, hour: int) -> datetime:
    """Next wall-clock ``weekday`` (1=Mon..7=Sun) at ``hour``:00 Athens, as UTC-aware datetime."""
    from zoneinfo import ZoneInfo
    ath = ZoneInfo("Europe/Athens")
    now_ath = datetime.now(timezone.utc).astimezone(ath)
    target = now_ath.replace(hour=hour, minute=0, second=0, microsecond=0)
    while True:
        if target.isoweekday() == weekday and target > now_ath:
            return target.astimezone(timezone.utc)
        target += timedelta(days=1)


# ── seed ──────────────────────────────────────────────────────

def seed_jobs(db) -> None:
    """Idempotently insert registry defaults; skip already-present names.
    New rows get next_run_at = now + interval so they don't all fire at once."""
    with db.cursor() as cur:
        cur.execute("SELECT name FROM cron_jobs")
        existing = {row[0] for row in cur.fetchall()}

    added = 0
    for name, (_path, interval, mode) in JOB_REGISTRY.items():
        if name in existing:
            continue
        if name in _FIXED_ATHENS_WEEKDAY:
            wd, hr = _FIXED_ATHENS_WEEKDAY[name]
            nxt = _next_athens_weekday_hour(wd, hr)
        elif name in _FIXED_ATHENS_HOUR:
            nxt = _next_athens_hour(_FIXED_ATHENS_HOUR[name])
        else:
            nxt = _now() + timedelta(seconds=interval)
        with db.cursor() as cur:
            cur.execute(
                """INSERT INTO cron_jobs (name, schedule_seconds, mode, enabled, next_run_at, updated_at)
                   VALUES (%s, %s, %s, TRUE, %s, %s)""",
                (name, interval, mode, nxt, _now()),
            )
        added += 1

    if added:
        db.commit()


# ── run ───────────────────────────────────────────────────────

def run_job(db, name: str) -> dict:
    """Run one job as a subprocess, record CronRun, advance next_run_at."""
    entry = JOB_REGISTRY.get(name)
    if entry is None:
        raise KeyError(f"Unknown cron job: {name}")
    script_path, interval, default_mode = entry

    # Concurrency guard — skip if a recent 'running' CronRun exists
    with db.cursor() as cur:
        cur.execute(
            """SELECT started_at FROM cron_runs
               WHERE job_name = %s AND status = 'running'
               ORDER BY started_at DESC LIMIT 1""",
            (name,),
        )
        row = cur.fetchone()
        if row and row[0] and row[0] > _now() - _RUNNING_STALE:
            return {"name": name, "status": "skipped", "summary": "already running", "duration_ms": 0}
        # Clean up stale running runs (crashed/restarted container)
        if row:
            cur.execute(
                "UPDATE cron_runs SET status='error', summary='killed: stale run (>3h)' WHERE job_name=%s AND status='running'",
                (name,),
            )
            db.commit()

    # Read live mode from DB (may differ from registry default)
    with db.cursor() as cur:
        cur.execute("SELECT mode FROM cron_jobs WHERE name = %s", (name,))
        mode_row = cur.fetchone()
        mode = mode_row[0] if mode_row else default_mode

    started = _now()
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO cron_runs (job_name, status, started_at) VALUES (%s, %s, %s) RETURNING id",
            (name, "running", started),
        )
        run_id = cur.fetchone()[0]
    db.commit()

    full_path = ROOT / script_path
    if not full_path.is_file():
        return _finish_run(db, run_id, name, "error", f"script_not_found: {full_path}", started, interval)

    env = os.environ.copy()
    env["AITRADER_MODE"] = mode

    output = ""
    try:
        result = subprocess.run(
            [sys.executable, str(full_path)],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=interval * 2,
        )
        ok = result.returncode == 0
        status = "ok" if ok else "error"
        output = (result.stdout + result.stderr).strip()
        summary = output[-2000:] if len(output) > 2000 else output
        if not summary:
            summary = "completed" if ok else f"exit code {result.returncode}"
    except subprocess.TimeoutExpired:
        status = "error"
        summary = f"timeout after {interval * 2}s"
        output = summary
    except Exception as e:
        status = "error"
        summary = f"{type(e).__name__}: {e}"
        output = summary

    # Durable full transcript (DB summary is truncated)
    header = (
        f"=== {started.isoformat()} job={name} mode={mode} "
        f"status={status} run_id={run_id} ==="
    )
    append_job_log(name, header, output)
    log.info("job=%s status=%s run_id=%s", name, status, run_id)

    return _finish_run(db, run_id, name, status, summary, started, interval)


def _finish_run(db, run_id: int, name: str, status: str, summary: str,
                started: datetime, interval: int) -> dict:
    finished = _now()
    duration_ms = int((finished - started).total_seconds() * 1000)
    summary = summary[:4000]

    with db.cursor() as cur:
        cur.execute(
            """UPDATE cron_runs
               SET status = %s, finished_at = %s, summary = %s, duration_ms = %s
               WHERE id = %s""",
            (status, finished, summary, duration_ms, run_id),
        )
        if name in _FIXED_ATHENS_WEEKDAY:
            wd, hr = _FIXED_ATHENS_WEEKDAY[name]
            nxt = _next_athens_weekday_hour(wd, hr)
        elif name in _FIXED_ATHENS_HOUR:
            nxt = _next_athens_hour(_FIXED_ATHENS_HOUR[name])
        else:
            nxt = _now() + timedelta(seconds=interval)
        cur.execute(
            """UPDATE cron_jobs
               SET next_run_at = %s, updated_at = %s
               WHERE name = %s""",
            (nxt, _now(), name),
        )
    db.commit()

    # ── Telegram notification ────────────────────────────────
    if summary and summary not in ("completed",):
        if name in _TRADE_SIGNAL_JOBS:
            # Only notify on real trade signals
            if not any(kw in summary for kw in _TRADE_SIGNAL_KEYWORDS):
                pass  # silent — no trade happened
            else:
                try:
                    send_telegram(f"🤖 {name}\n{summary[:3800]}")
                except Exception:
                    pass
        else:
            # Non-trading jobs: always notify
            try:
                send_telegram(f"🤖 {name}\n{summary[:3800]}")
            except Exception:
                pass

    return {"name": name, "status": status, "summary": summary, "duration_ms": duration_ms}


def _defer_job(db, name: str, next_run_at: datetime, reason: str) -> dict:
    """Move a conditionally-scheduled job forward without creating a run."""
    with db.cursor() as cur:
        cur.execute(
            "UPDATE cron_jobs SET next_run_at=%s, updated_at=%s WHERE name=%s",
            (next_run_at, _now(), name),
        )
    db.commit()
    log.info("job=%s deferred until=%s reason=%s", name, next_run_at.isoformat(), reason)
    return {"name": name, "status": "skipped", "summary": reason, "duration_ms": 0}


# ── tick ──────────────────────────────────────────────────────

def tick(db) -> dict:
    """Seed defaults, then run every enabled job where next_run_at <= now."""
    seed_jobs(db)
    now = _now()
    ran = []
    errors: dict[str, str] = {}

    with db.cursor() as cur:
        cur.execute(
            "SELECT name FROM cron_jobs WHERE enabled = TRUE AND mode != 'paused' AND next_run_at <= %s",
            (now,),
        )
        jobs = [row[0] for row in cur.fetchall()]

    for name in jobs:
        try:
            if name == "alpaca-stocks":
                market_open, next_open, reason = alpaca_market_status()
                if not market_open:
                    interval = JOB_REGISTRY[name][1]
                    res = _defer_job(db, name, deferred_next_run(next_open, interval), reason)
                    continue
            res = run_job(db, name)
            if res.get("status") != "skipped":
                ran.append(name)
        except Exception as e:
            errors[name] = f"{type(e).__name__}: {e}"

    return {"ran": ran, "errors": errors, "checked": len(jobs)}


# ── list ──────────────────────────────────────────────────────

def list_jobs(db) -> list[dict]:
    """Return all cron_jobs rows for the UI."""
    with db.cursor() as cur:
        cur.execute(
            """SELECT name, schedule_seconds, mode, enabled, next_run_at, updated_at
               FROM cron_jobs ORDER BY name"""
        )
        cols = ["name", "schedule_seconds", "mode", "enabled", "next_run_at", "updated_at"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


# ── CLI ───────────────────────────────────────────────────────

def main() -> int:
    from app.db import get_conn

    if len(sys.argv) < 2:
        print("usage: cron_orchestrator.py <tick|run <name>|list>", file=sys.stderr)
        return 1

    cmd = sys.argv[1]

    with get_conn() as conn:
        if cmd == "tick":
            result = tick(conn)
            print(result)
        elif cmd == "run":
            if len(sys.argv) < 3:
                print("usage: cron_orchestrator.py run <name>", file=sys.stderr)
                return 1
            result = run_job(conn, sys.argv[2])
            print(result)
        elif cmd == "list":
            for job in list_jobs(conn):
                print(job)
        else:
            print(f"unknown command: {cmd}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
