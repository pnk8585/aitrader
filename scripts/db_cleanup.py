"""DB cleanup cron job — asset_prices downsampling + old rows purge + VACUUM.

Runs inside the aitrader container via cron_orchestrator (job: db-cleanup).
Target wall time: ~05:00 Athens (next_run advanced to next 05:00 after each run).
"""

from __future__ import annotations

import sys
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import get_conn

# Retention windows
CRON_RUNS_RETENTION_HOURS = 24   # orchestrator history — keep 1 day only
TRADE_LOG_RETENTION_DAYS = 180

ATHENS = ZoneInfo("Europe/Athens")
NOW = datetime.now(timezone.utc)
TS = NOW.strftime("%d/%m/%Y %H:%M")


def _cleanup_asset_prices(cur) -> tuple[int, int]:
    """Downsample asset_prices: keep 1 row/hour/symbol for data >24h old."""
    cur.execute("SELECT DISTINCT symbol FROM asset_prices ORDER BY symbol")
    symbols = [r[0] for r in cur.fetchall()]

    total_deleted = 0

    for symbol in symbols:
        cur.execute(
            "SELECT COUNT(*) FROM asset_prices WHERE symbol = %s "
            "AND timestamp >= NOW() - INTERVAL '24 hours'",
            (symbol,),
        )
        recent = cur.fetchone()[0]

        cur.execute(
            """SELECT id FROM asset_prices
               WHERE symbol = %s
               AND timestamp < NOW() - INTERVAL '24 hours'
               AND id NOT IN (
                   SELECT DISTINCT ON (date_trunc('hour', timestamp)) id
                   FROM asset_prices
                   WHERE symbol = %s
                   AND timestamp < NOW() - INTERVAL '24 hours'
                   ORDER BY date_trunc('hour', timestamp), timestamp ASC
               )""",
            (symbol, symbol),
        )
        ids_to_delete = [r[0] for r in cur.fetchall()]

        if ids_to_delete:
            cur.execute("DELETE FROM asset_prices WHERE id = ANY(%s)", (ids_to_delete,))
            deleted = cur.rowcount
            total_deleted += deleted
            print(f"📊 {symbol}: deleted {deleted} | recent 24h: {recent}")
        else:
            print(f"✅ {symbol}: clean | recent 24h: {recent}")

    cur.execute("SELECT COUNT(*) FROM asset_prices")
    remaining = cur.fetchone()[0]
    return total_deleted, remaining


def _delete_older_than_hours(cur, table: str, hours: int, ts_col: str) -> tuple[int, int, int]:
    """Delete rows older than `hours`, return (before, deleted, after)."""
    cutoff = NOW - timedelta(hours=hours)
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    before = cur.fetchone()[0]
    cur.execute(f"DELETE FROM {table} WHERE {ts_col} < %s", (cutoff,))
    deleted = cur.rowcount
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    after = cur.fetchone()[0]
    return before, deleted, after


def _delete_older_than_days(cur, table: str, days: int, ts_col: str) -> tuple[int, int, int]:
    return _delete_older_than_hours(cur, table, days * 24, ts_col)


def main() -> int:
    try:
        with get_conn() as conn:
            conn.autocommit = False

            with conn.cursor() as cur:
                print(f"🗓️  DB Cleanup — {TS} UTC")
                print("-" * 50)

                # ── asset_prices: downsampling ──────────────────
                print("\n📊 **asset_prices** (downsample >24h to 1/hour)")
                prices_deleted, prices_remaining = _cleanup_asset_prices(cur)

                # ── cron_runs: 24h retention ────────────────────
                before, deleted, after = _delete_older_than_hours(
                    cur, "cron_runs", CRON_RUNS_RETENTION_HOURS, "started_at"
                )
                print(
                    f"\n📋 **cron_runs**: {before} → {after} "
                    f"(deleted {deleted} older than {CRON_RUNS_RETENTION_HOURS}h)"
                )

                # ── trade_log ───────────────────────────────────
                before, deleted, after = _delete_older_than_days(
                    cur, "trade_log", TRADE_LOG_RETENTION_DAYS, "timestamp"
                )
                print(
                    f"📋 **trade_log**: {before} → {after} "
                    f"(deleted {deleted} older than {TRADE_LOG_RETENTION_DAYS}d)"
                )

            conn.commit()

            # ── VACUUM (outside transaction) ────────────────────
            conn.autocommit = True
            print("\n🧹 **VACUUM ANALYZE**")
            with conn.cursor() as cur:
                for table in ("cron_runs", "asset_prices", "trade_log", "trading_state", "cron_jobs"):
                    cur.execute(f"VACUUM ANALYZE {table}")
                    print(f"  ✅ {table}")

            print("-" * 50)
            print(f"🧹 asset_prices: deleted {prices_deleted:,} | remaining {prices_remaining:,}")
            print("✅ Done")

            return 0

    except Exception as e:
        print(f"\n❌ db_cleanup error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
