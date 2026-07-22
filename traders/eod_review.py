#!/usr/bin/env python3
"""End-of-Day trading summary — queries DB and prints structured report.

Called by cron_orchestrator.py at 20:00 UTC daily.
Output goes to stdout → captured as CronRun summary.
"""

from __future__ import annotations

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import get_conn
from datetime import date


def main() -> None:
    today = date.today()
    with get_conn() as conn:
        cur = conn.cursor()

        # Today's trades
        cur.execute(
            "SELECT COUNT(*), COALESCE(SUM(unrealized_plpc), 0) FROM trade_log WHERE timestamp::date = %s",
            (today,),
        )
        trades_today, pl_today = cur.fetchone()

        # Open positions
        cur.execute("SELECT COUNT(*), COALESCE(SUM(peak_plpc), 0) FROM trading_state")
        open_count, open_pl = cur.fetchone()

        # Today's LLM reviews
        cur.execute(
            "SELECT COUNT(*), COUNT(*) FILTER (WHERE verdict='buy'), COUNT(*) FILTER (WHERE verdict='sell') FROM llm_review_log WHERE created_at::date = %s",
            (today,),
        )
        total_reviews, buys, sells = cur.fetchone()

        # Cron runs today
        cur.execute(
            "SELECT COUNT(*), COUNT(*) FILTER (WHERE status='ok'), COUNT(*) FILTER (WHERE status='error') FROM cron_runs WHERE started_at::date = %s",
            (today,),
        )
        runs_total, runs_ok, runs_err = cur.fetchone()

    report = f"""AITrader EOD Report — {today}
──────────────────────────────────
Trades today: {trades_today} (P/L: {pl_today:.2f}%)
Open positions: {open_count} (P/L: {open_pl:.2f}%)
LLM reviews: {total_reviews} (buy: {buys}, sell: {sells})
Cron runs: {runs_total} (ok: {runs_ok}, err: {runs_err})"""
    print(report)


if __name__ == "__main__":
    main()
