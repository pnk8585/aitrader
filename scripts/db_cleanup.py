"""DB cleanup cron job — purge old rows and vacuum.
Runs inside the aitrader container via cron_orchestrator.
"""

from __future__ import annotations

import sys
import os
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import get_conn

# Retention windows
CRON_RUNS_RETENTION_DAYS = 30
ASSET_PRICES_RETENTION_DAYS = 14
TRADE_LOG_RETENTION_DAYS = 180


def _count_rows(cur, table: str) -> int:
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    return cur.fetchone()[0]


def _delete_older_than(cur, table: str, days: int, ts_col: str = "timestamp") -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cur.execute(
        f"DELETE FROM {table} WHERE {ts_col} < %s",
        (cutoff,),
    )
    return cur.rowcount


def main() -> int:
    results: list[str] = []

    try:
        with get_conn() as conn:
            conn.autocommit = False

            with conn.cursor() as cur:
                # ── cron_runs (started_at) ──────────────────────
                before = _count_rows(cur, "cron_runs")
                deleted = _delete_older_than(cur, "cron_runs", CRON_RUNS_RETENTION_DAYS, "started_at")
                after = _count_rows(cur, "cron_runs")
                results.append(
                    f"cron_runs: {before} → {after} rows (deleted {deleted} older than {CRON_RUNS_RETENTION_DAYS}d)"
                )

                # ── asset_prices (timestamp) ────────────────────
                before = _count_rows(cur, "asset_prices")
                deleted = _delete_older_than(cur, "asset_prices", ASSET_PRICES_RETENTION_DAYS)
                after = _count_rows(cur, "asset_prices")
                results.append(
                    f"asset_prices: {before} → {after} rows (deleted {deleted} older than {ASSET_PRICES_RETENTION_DAYS}d)"
                )

                # ── trade_log (timestamp) ───────────────────────
                before = _count_rows(cur, "trade_log")
                deleted = _delete_older_than(cur, "trade_log", TRADE_LOG_RETENTION_DAYS)
                after = _count_rows(cur, "trade_log")
                results.append(
                    f"trade_log: {before} → {after} rows (deleted {deleted} older than {TRADE_LOG_RETENTION_DAYS}d)"
                )

            conn.commit()

            # ── VACUUM (must be outside transaction) ────────────
            conn.autocommit = True
            with conn.cursor() as cur:
                for table in ("cron_runs", "asset_prices", "trade_log", "trading_state", "cron_jobs"):
                    cur.execute(f"VACUUM ANALYZE {table}")
                    results.append(f"VACUUM ANALYZE {table}: ok")

            summary = " | ".join(results)
            print(summary)
            return 0

    except Exception as e:
        print(f"db_cleanup error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
