#!/usr/bin/env python3
"""hourly_report.py — Combined AITrader status report every hour (container cron).

Runs inside the container (JOB_REGISTRY "hourly-report", 3600s). Reads from the
same DB via app.db.get_conn. No host dependencies (no ccxt, no dotenv).

Queries:
  1. Container cron health — per-job runs/errors/staleness from cron_runs (24h)
  2. Alpaca account snapshot — REST API (equity, buying power, positions)
  3. Kraken account snapshot — DB trading_state + latest asset_prices

Outputs a single compact message → captured as CronRun summary → delivered by
the orchestrator (non-trade job → always notify).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from app.db import get_conn
from app.wallets import kraken_balance

# ---- Alpaca -----------------------------------------------------------------
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://api.alpaca.markets")

# Container cron jobs we care about (DB cron_jobs names)
AITRADER_JOBS = [
    "kraken-pullback",
    "kraken-grid",
    "kraken-momentum",
    "kraken-high-risk",
    "alpaca-stocks",
    "position-monitor",
]

EXPECTED_INTERVAL = {
    "kraken-pullback": 300,
    "kraken-grid": 300,
    "kraken-momentum": 300,
    "kraken-high-risk": 300,
    "alpaca-stocks": 300,
    "position-monitor": 7200,
}

STALE_FACTOR = 3
MARKET_WINDOWED = {"alpaca-stocks"}


def get_cron_status() -> str:
    """Query cron_runs for the last 24h — per-job health from the container DB."""
    parts = []
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """SELECT job_name, count(*) AS runs,
                          count(*) FILTER (WHERE status = 'error') AS errs,
                          max(started_at) AS last_run
                   FROM cron_runs
                   WHERE started_at > %s
                   GROUP BY job_name""",
                (datetime.now(timezone.utc) - timedelta(hours=24),),
            )
            per_job = {r[0]: {"runs": r[1], "errs": r[2], "last": r[3]} for r in cur.fetchall()}

        for job in AITRADER_JOBS:
            meta = per_job.get(job)
            if not meta or meta["runs"] == 0:
                if job in MARKET_WINDOWED:
                    parts.append(f"⏸️ {job}")
                else:
                    parts.append(f"❌ {job}")
                continue

            errs = meta["errs"]
            interval = EXPECTED_INTERVAL.get(job, 300)
            stale = bool(meta["last"]) and (datetime.now(timezone.utc) - meta["last"]) > timedelta(seconds=interval * STALE_FACTOR)

            if errs > 0:
                parts.append(f"❌ {job} ({errs} err)")
            elif stale and job not in MARKET_WINDOWED:
                parts.append(f"⚠️ {job} stale")
            else:
                parts.append(f"✅ {job}")

        return " | ".join(parts) if parts else "⚠️ No AITrader crons found"
    except Exception as e:
        return f"⚠️ DB error: {e}"


def get_alpaca_status() -> str:
    """Fetch Alpaca account snapshot."""
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return "⚠️ Alpaca: no API keys"

    headers = {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }

    try:
        acc_res = requests.get(
            f"{ALPACA_BASE_URL}/v2/account", headers=headers, timeout=10
        )
        if acc_res.status_code != 200:
            return f"⚠️ Alpaca API error: {acc_res.status_code}"

        acc = acc_res.json()
        equity = float(acc.get("equity", 0.0))
        buying_power = float(acc.get("buying_power", 0.0))

        pos_res = requests.get(
            f"{ALPACA_BASE_URL}/v2/positions", headers=headers, timeout=10
        )
        positions = pos_res.json() if pos_res.status_code == 200 else []

        if not positions:
            return f"💰 ${round(equity, 2)} · ${round(buying_power, 2)} free · 0 pos"

        pos_lines = []
        for p in positions:
            sym = p["symbol"]
            pl = float(p["unrealized_plpc"])
            pos_lines.append(f"{sym} {round(pl, 1)}%")

        pos_str = " | ".join(pos_lines)
        return f"💰 ${round(equity, 2)} · ${round(buying_power, 2)} free · {len(positions)} pos · {pos_str}"

    except Exception as e:
        return f"⚠️ Alpaca fetch error: {e}"


def get_kraken_status() -> str:
    """Live Kraken EUR balance plus DB-backed strategy positions and prices."""
    try:
        wallet = kraken_balance()
        if wallet.get("error"):
            return f"⚠️ Kraken balance error: {wallet['error']}"
        cash = f"€{float(wallet.get('free_eur', 0.0)):.2f} free"

        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """SELECT symbol, quantity, entry_price FROM trading_state
                   WHERE exchange LIKE 'kraken%' ORDER BY symbol"""
            )
            positions = cur.fetchall()

            cur.execute(
                """SELECT DISTINCT ON (symbol) symbol, price
                   FROM asset_prices WHERE exchange = 'kraken'
                   ORDER BY symbol, timestamp DESC"""
            )
            prices = {r[0]: r[1] for r in cur.fetchall()}

        if not positions:
            return f"💰 {cash} · 0 pos (DB)"

        pos_lines = []
        total_value = 0.0
        for sym, qty, entry in positions:
            sym = sym.split("/")[0].upper()  # 'DOGE/EUR' -> 'DOGE' (asset_prices uses bare symbol)
            price = prices.get(sym)
            if price is None:
                pos_lines.append(f"{sym} ?")
                continue
            value = float(qty) * float(price)
            total_value += value
            if entry:
                pl_pct = (float(price) - float(entry)) / float(entry) * 100
                pos_lines.append(f"{sym} {value:.2f}€ {pl_pct:+.1f}%")
            else:
                pos_lines.append(f"{sym} {value:.2f}€")

        pos_str = " | ".join(pos_lines)
        return f"💰 {cash} · {total_value:.2f}€ in {len(positions)} pos · {pos_str}"

    except Exception as e:
        return f"⚠️ Kraken DB error: {e}"


def main():
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")

    cron_status = get_cron_status()
    alpaca_status = get_alpaca_status()
    kraken_status = get_kraken_status()

    print(f"🕐 {now}")
    print(f"📡 {cron_status}")
    print(f"🏛️ Alpaca  {alpaca_status}")
    print(f"🪙 Kraken  {kraken_status}")


if __name__ == "__main__":
    main()
