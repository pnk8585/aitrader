#!/usr/bin/env python3
"""P&L attribution dashboard from trade_log — per strategy and exit reason."""

import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import psycopg2
from dotenv import load_dotenv

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
load_dotenv(os.path.join(ROOT, ".env"))


def connect():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.getenv("DB_NAME", "aitrader"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD", ""),
        connect_timeout=5,
    )


def main():
    days = int(os.getenv("PNL_DAYS", "7"))
    since = datetime.now(timezone.utc) - timedelta(days=days)

    conn = connect()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT exchange, action, ticker, unrealized_plpc, reason, timestamp
        FROM trade_log
        WHERE timestamp >= %s
        ORDER BY timestamp DESC
        """,
        (since,),
    )
    rows = cur.fetchall()
    conn.close()

    by_exchange = defaultdict(lambda: {"buys": 0, "sells": 0, "reasons": defaultdict(int)})
    sell_plpc = defaultdict(list)

    for exchange, action, ticker, plpc, reason, ts in rows:
        bucket = by_exchange[exchange]
        if action == "BUY":
            bucket["buys"] += 1
        elif action == "SELL":
            bucket["sells"] += 1
            if plpc is not None:
                sell_plpc[exchange].append(float(plpc) * 100.0)
            key = (reason or "unknown")[:60]
            bucket["reasons"][key] += 1

    print(f"=== P&L Dashboard (last {days} days, since {since.date()}) ===\n")
    if not rows:
        print("No trades in window.")
        return

    for ex, data in sorted(by_exchange.items()):
        sells = sell_plpc.get(ex, [])
        avg = sum(sells) / len(sells) if sells else 0.0
        wins = sum(1 for x in sells if x > 0)
        wr = (wins / len(sells) * 100) if sells else 0.0
        print(f"## {ex}")
        print(f"  Trades: {data['buys']} buys / {data['sells']} sells")
        print(f"  Sell P/L: avg {avg:+.2f}% | win rate {wr:.0f}% ({wins}/{len(sells)})")
        if data["reasons"]:
            print("  Top exit reasons:")
            for reason, count in sorted(data["reasons"].items(), key=lambda x: -x[1])[:5]:
                print(f"    - [{count}x] {reason}")
        print()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"pnl_dashboard failed: {e}", file=sys.stderr)
        sys.exit(1)