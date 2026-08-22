#!/usr/bin/env python3
"""LLM verdict backfill — closes the learning loop of the LLM trade review.

Fills `pnl_1h_pct` / `pnl_6h_pct` / `pnl_24h_pct` and `was_correct` on
`llm_review_log` by comparing the price at verdict time (`price` column,
recorded by traders/common/llm_review.py::_log_review) with the nearest
`asset_prices` sample taken ~1h / ~6h / ~24h AFTER `created_at`.

Runs inside the aitrader container as cron job `llm-backfill` (3600s, live)
via the JOB_REGISTRY entry in app/cron_orchestrator.py. Standalone on the
host for verification: `python3 scripts/llm_backfill.py --dry-run`.

Design decisions
────────────────
* **Nearest sample, bounded staleness.** For each horizon we take the
  asset_prices row closest to `created_at + horizon`
  (`ORDER BY ABS(EXTRACT(EPOCH FROM (timestamp - target))) LIMIT 1`), but
  only within ±MAX_STALENESS of the target; a sample further away is
  treated as "no data yet" and the horizon stays NULL.
* **Terminal states (no infinite retries).** A row leaves the pending set
  (`pnl_24h_pct IS NULL`) in exactly one of two ways:
    - the 24h horizon is computed → real forward PnL, or
    - sentinel `pnl_24h_pct = -999` → the symbol has no asset_prices feed
      at all (US stocks), or the row is older than MAX_AGE and the 24h
      sample never appeared (multi-hour recording gap). Sentinel rows get
      `was_correct = NULL`.
* **was_correct** (per spec):
    APPROVE → True if pnl_24h > +fee, False if pnl_24h < 0, else NULL
    REJECT  → True if pnl_24h < 0, False if pnl_24h > +fee, else NULL
    HOLD / unknown verdict / missing 24h data → NULL
  Values inside the grey zone (0 ≤ pnl ≤ fee) stay NULL — a trade that
  only covers fees proves nothing.
* **Fee threshold by strategy family** (round trip):
    crypto (pullback / momentum / high-risk / grid) = 0.52% (Kraken 0.26% × 2)
    stocks / alpaca = 0.01%; unknown family → 0.52% (conservative, Kraken)
* **DB driver:** psycopg2 with `autocommit=True` (HERMES.md "DB driver" —
  avoids InFailedSqlTransaction cascades).
* **Silent when idle:** in cron mode prints nothing when no rows are
  pending, so the orchestrator stores "completed" and skips the hourly
  Telegram notification. `--dry-run` always prints its summary.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

import psycopg2
from dotenv import load_dotenv

# ── Config ─────────────────────────────────────────────────────────────

# Round-trip fee thresholds (%) per strategy family.
CRYPTO_FEE_THRESHOLD = 0.52   # Kraken taker 0.26% × 2 legs
STOCK_FEE_THRESHOLD = 0.01    # Alpaca commission-free, spread only

# Crypto family keywords, matched after the stocks/alpaca check so that
# "stocks-momentum" never matches the crypto "momentum" keyword.
CRYPTO_KEYWORDS = ("pullback", "momentum", "high-risk", "grid")

# Sentinel written to pnl_24h_pct for rows that can never get price data.
SENTINEL_NO_DATA = -999.0

# A sample only counts for a horizon if recorded within ±MAX_STALENESS of
# the target time; otherwise the horizon stays NULL ("no data yet").
MAX_STALENESS = timedelta(hours=4)

# Rows older than this whose 24h sample never appeared are sentineled so
# they stop being retried (covers long price-recording gaps).
MAX_AGE = timedelta(hours=48)

HORIZONS = (
    ("pnl_1h_pct", timedelta(hours=1)),
    ("pnl_6h_pct", timedelta(hours=6)),
    ("pnl_24h_pct", timedelta(hours=24)),
)


def _connect():
    """Same connection pattern as traders/common/llm_review.py::_log_review.

    On the host the values come from the project .env; inside the container
    they are already present in the docker environment (load_dotenv is a
    no-op there — override=False never clobbers existing vars).
    """
    load_dotenv("/home/pank/projects/aitrader/.env", override=False)
    conn = psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", 5432)),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        connect_timeout=5,
    )
    conn.autocommit = True  # HERMES.md "DB driver"
    return conn


def _fee_threshold(strategy: str) -> float:
    """Round-trip fee threshold for a strategy family."""
    s = (strategy or "").lower()
    if "stock" in s or "alpaca" in s:
        return STOCK_FEE_THRESHOLD
    if any(k in s for k in CRYPTO_KEYWORDS):
        return CRYPTO_FEE_THRESHOLD
    return CRYPTO_FEE_THRESHOLD  # unknown family (e.g. position-monitor) → Kraken fees


def _base_symbol(symbol: str) -> str:
    """'SOL/EUR' → 'SOL' (asset_prices stores the base coin); stocks pass through."""
    return symbol.split("/")[0]


def _was_correct(verdict: str, pnl_24h: float | None, fee: float) -> bool | None:
    """Score a verdict against its 24h forward PnL (None = not scoreable)."""
    if pnl_24h is None or pnl_24h == SENTINEL_NO_DATA:
        return None
    if verdict == "APPROVE":
        if pnl_24h > fee:
            return True
        if pnl_24h < 0:
            return False
        return None  # grey zone: 0 ≤ pnl ≤ fee
    if verdict == "REJECT":
        if pnl_24h < 0:
            return True
        if pnl_24h > fee:
            return False
        return None  # grey zone
    return None  # HOLD / ROTATE / unknown → not scoreable


def backfill(conn, dry_run: bool) -> dict:
    """One backfill pass over all pending rows. Returns the stats dict."""
    stats = {"processed": 0, "backfilled": 0, "partial": 0,
             "no_data": 0, "waiting": 0, "skipped": 0}
    cur = conn.cursor()
    cur.execute(
        """SELECT id, strategy, symbol, price, verdict, created_at
           FROM llm_review_log
           WHERE pnl_24h_pct IS NULL
           ORDER BY created_at"""
    )
    rows = cur.fetchall()
    now = datetime.now(timezone.utc)
    has_data_cache: dict[str, bool] = {}
    updates: list[tuple] = []

    for row_id, strategy, symbol, price, verdict, created_at in rows:
        stats["processed"] += 1
        base = _base_symbol(symbol)
        fee = _fee_threshold(strategy)

        if base not in has_data_cache:
            cur.execute(
                "SELECT 1 FROM asset_prices WHERE symbol = %s LIMIT 1", (base,))
            has_data_cache[base] = cur.fetchone() is not None

        if not has_data_cache[base]:
            # No price feed for this symbol (e.g. US stocks) — sentinel
            # moves the row out of the pending set permanently.
            updates.append((None, None, SENTINEL_NO_DATA, None, row_id))
            stats["no_data"] += 1
            if dry_run:
                print(f"  [{row_id}] {symbol} ({strategy}): "
                      f"no price feed for '{base}' → sentinel -999")
            continue

        pnls: dict[str, float | None] = {}
        for col, delta in HORIZONS:
            target = created_at + delta
            if target > now:
                pnls[col] = None  # horizon not reached yet — next run
                continue
            cur.execute(
                """SELECT price FROM asset_prices
                   WHERE symbol = %s
                     AND timestamp BETWEEN %s AND %s
                   ORDER BY ABS(EXTRACT(EPOCH FROM (timestamp - %s)))
                   LIMIT 1""",
                (base, target - MAX_STALENESS, target + MAX_STALENESS, target),
            )
            res = cur.fetchone()
            if res and price and float(price) > 0:
                pnls[col] = (float(res[0]) - float(price)) / float(price) * 100
            else:
                pnls[col] = None

        pnl_24h = pnls["pnl_24h_pct"]

        if pnl_24h is None and created_at < now - MAX_AGE:
            # Old enough, but the 24h sample never appeared (data gap) —
            # sentinel, otherwise this row would be retried forever.
            updates.append((pnls["pnl_1h_pct"], pnls["pnl_6h_pct"],
                            SENTINEL_NO_DATA, None, row_id))
            stats["no_data"] += 1
            if dry_run:
                print(f"  [{row_id}] {symbol} ({strategy}): "
                      f"24h sample missing, row >{MAX_AGE // timedelta(hours=1)}h old → sentinel -999")
            continue

        correct = _was_correct(verdict, pnl_24h, fee)
        updates.append((pnls["pnl_1h_pct"], pnls["pnl_6h_pct"], pnl_24h,
                        correct, row_id))

        if pnl_24h is not None:
            stats["backfilled"] += 1
        elif pnls["pnl_1h_pct"] is not None or pnls["pnl_6h_pct"] is not None:
            stats["partial"] += 1
        else:
            stats["waiting"] += 1

        if dry_run:
            bits = " ".join(
                f"{h}={pnls[h]:+.2f}%"
                for h in ("pnl_1h_pct", "pnl_6h_pct", "pnl_24h_pct")
                if pnls[h] is not None)
            suffix = f" correct={correct}" if correct is not None else ""
            print(f"  [{row_id}] {symbol} ({strategy}) {verdict}: "
                  f"{bits or 'waiting (horizon not reached)'}{suffix}")

    if not dry_run:
        for pnl_1h, pnl_6h, pnl_24h, correct, row_id in updates:
            cur.execute(
                """UPDATE llm_review_log
                   SET pnl_1h_pct = %s, pnl_6h_pct = %s,
                       pnl_24h_pct = %s, was_correct = %s
                   WHERE id = %s""",
                (pnl_1h, pnl_6h, pnl_24h, correct, row_id),
            )
    cur.close()
    return stats


def format_summary(stats: dict, *, dry_run: bool = False) -> str:
    """Return a compact, human-readable Telegram report."""
    prefix = "🧪 DRY RUN · " if dry_run else ""
    return (
        f"{prefix}📦 Επεξεργάστηκαν: {stats['processed']}\n"
        f"✅ Ολοκληρώθηκαν 24h: {stats['backfilled']}\n"
        f"⏳ Μερικά δεδομένα (1h/6h): {stats['partial']}\n"
        f"🚫 Χωρίς price data: {stats['no_data']}\n"
        f"🕒 Σε αναμονή: {stats['waiting']}\n"
        f"⏭️ Παραλείφθηκαν: {stats['skipped']}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill llm_review_log pnl_*_pct + was_correct from asset_prices")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be done without UPDATEs")
    args = parser.parse_args()

    try:
        conn = _connect()
    except Exception as e:
        print(f"llm-backfill: DB connection failed: {e}", file=sys.stderr)
        return 1

    try:
        stats = backfill(conn, dry_run=args.dry_run)
    except Exception as e:
        print(f"llm-backfill error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    if stats["processed"] == 0 and not args.dry_run:
        # Idle run → no output; the orchestrator stores "completed" and
        # skips the Telegram notification (no hourly spam).
        return 0

    print(format_summary(stats, dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
