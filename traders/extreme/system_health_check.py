"""System health check — queries PostgreSQL + live exchange APIs.

Replaced the old file-based checks (v1 state/heartbeat files) with DB queries
against trading_state, trade_log, notify_state, and asset_prices. Also fetches
live balances from Kraken and Alpaca.
"""

import os
import sys
import json
import ccxt
import requests
import psycopg2
from datetime import datetime, timezone
from dotenv import load_dotenv

# Thresholds (seconds)
NOTIFY_STALE_S = 3600       # 1h — heartbeat should fire at least hourly
ASSET_PRICE_STALE_S = 600   # 10 min — scripts insert prices every 5 min
TRADE_LOG_STALE_S = 7200    # 2h — no trade in 2h is OK for slow strategies
NO_TRADE_24H_WARN = 0       # zero trades in 24h is suspicious
DB_TIMEOUT_S = 5
EXPECTED_HEARTBEATS = {"kraken-momentum", "kraken-pullback", "alpaca-stocks", "ai_overseer"}


def connect_db():
    """Open a fresh DB connection."""
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
        user=os.getenv("DB_USER", "pank"),
        password=os.getenv("DB_PASSWORD", ""),
        dbname=os.getenv("DB_NAME", "trading"),
        connect_timeout=DB_TIMEOUT_S,
    )


def now_utc():
    return datetime.now(timezone.utc)


def elapsed_secs(dt):
    """Seconds since *dt* -- float('inf') if dt is None. Naive dts assumed UTC."""
    if dt is None:
        return float("inf")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now_utc() - dt).total_seconds()


def secs_label(secs):
    """Display label for elapsed seconds; survives float('inf') from NULL rows."""
    return "never" if secs == float("inf") else f"{round(secs)}s"


def status_ok(ok, detail=""):
    return {"ok": ok, "detail": detail}


def main():
    ROOT_DIR = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "../.."))
    env_path = os.path.join(ROOT_DIR, ".env")
    load_dotenv(dotenv_path=env_path)

    report = {
        "timestamp": now_utc().isoformat(),
        "database": {},
        "kraken_strategies": {},
        "alpaca_strategies": {},
        "live_balances": {},
        "summary": {"healthy": True, "warnings": []},
    }
    DEPRECATED_EXCHANGES = {"alpaca"}  # v1 names no longer in use

    # ------------------------------------------------------------------
    # 1. Database connection & table freshness
    # ------------------------------------------------------------------
    try:
        conn = connect_db()
        report["database"]["connect"] = status_ok(True)
    except Exception as e:
        report["database"]["connect"] = status_ok(False, str(e))
        report["summary"]["healthy"] = False
        report["summary"]["warnings"].append("DB connection failed")
        print(json.dumps(report, indent=2))
        return

    try:
        with conn.cursor() as cur:
            # --- asset_prices freshness ---
            cur.execute(
                "SELECT MAX(timestamp) FROM asset_prices WHERE exchange='kraken'")
            row = cur.fetchone()
            last_price_ts = row[0] if row else None
            secs = elapsed_secs(last_price_ts)
            report["database"]["price_feed"] = status_ok(
                secs < ASSET_PRICE_STALE_S,
                f"last price {secs_label(secs)} ago"
            )
            if secs >= ASSET_PRICE_STALE_S:
                report["summary"]["warnings"].append(
                    f"Price feed stale ({secs_label(secs)} since last insert)")

            # --- trade_log freshness ---
            cur.execute("SELECT MAX(timestamp) FROM trade_log")
            row = cur.fetchone()
            last_trade_ts = row[0] if row else None
            secs = elapsed_secs(last_trade_ts)
            report["database"]["last_trade"] = status_ok(
                secs < TRADE_LOG_STALE_S,
                f"last trade {secs_label(secs)} ago"
            )
            if secs >= TRADE_LOG_STALE_S:
                report["summary"]["warnings"].append(
                    f"No trade in {secs_label(secs)}")

            cur.execute(
                "SELECT COUNT(*) FROM trade_log "
                "WHERE timestamp >= NOW() - INTERVAL '24 hours'")
            trades_24h = cur.fetchone()[0]
            report["database"]["trades_24h"] = trades_24h
            if trades_24h == NO_TRADE_24H_WARN:
                report["summary"]["warnings"].append(
                    "Zero trades in the last 24h")
    except Exception as e:
        report["database"]["query_error"] = str(e)
        report["summary"]["healthy"] = False
        report["summary"]["warnings"].append("DB query error")
        conn.close()
        print(json.dumps(report, indent=2))
        return

    # ------------------------------------------------------------------
    # 2. Notify-state freshness per exchange (heartbeat health)
    # ------------------------------------------------------------------
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT exchange, MAX(updated_at) FROM notify_state "
                "GROUP BY exchange ORDER BY exchange")
            seen_exchanges = set()
            for ex, ts in cur.fetchall():
                seen_exchanges.add(ex)
                secs = elapsed_secs(ts)
                ok = secs < NOTIFY_STALE_S
                if ex in DEPRECATED_EXCHANGES:
                    ok = True  # old v1 names, not actively updated
                report["database"].setdefault("heartbeats", {})[ex] = {
                    "ok": ok,
                    "since_s": round(secs) if secs != float("inf") else None,
                    "last": ts.isoformat() if ts else None,
                }
                if not ok:
                    report["summary"]["warnings"].append(
                        f"Exchange '{ex}' heartbeat stale ({secs_label(secs)})")
            for ex in sorted(EXPECTED_HEARTBEATS - seen_exchanges):
                report["database"].setdefault("heartbeats", {})[ex] = {
                    "ok": False, "since_s": None, "last": None,
                }
                report["summary"]["warnings"].append(
                    f"Exchange '{ex}' has no heartbeat row")
    except Exception as e:
        report["database"]["heartbeat_error"] = str(e)

    # ------------------------------------------------------------------
    # 3. Open positions from trading_state
    # ------------------------------------------------------------------
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT exchange, symbol, COALESCE(entry_price, 0), "
                "       EXTRACT(EPOCH FROM NOW() - entry_time) AS age_h "
                "FROM trading_state ORDER BY exchange, symbol")
            rows = cur.fetchall()
            open_positions = {}
            for ex, sym, entry, age_h in rows:
                open_positions.setdefault(ex, []).append({
                    "symbol": sym,
                    "entry": float(entry) if entry else 0,
                    "age_h": round(float(age_h), 1) if age_h is not None else 0,
                })
            report["database"]["open_positions"] = open_positions
    except Exception as e:
        report["database"]["positions_error"] = str(e)

    conn.close()

    # ------------------------------------------------------------------
    # 4. Live Kraken balance
    # ------------------------------------------------------------------
    kraken_key = os.getenv("KRAKEN_API_KEY")
    kraken_secret = os.getenv("KRAKEN_SECRET")
    if kraken_key and kraken_secret:
        try:
            exchange = ccxt.kraken({
                "apiKey": kraken_key,
                "secret": kraken_secret,
                "enableRateLimit": True,
            })
            balance = exchange.fetch_balance()
            symbols = [
                "BTC/EUR", "ETH/EUR", "SOL/EUR", "AVAX/EUR", "LINK/EUR",
                "XRP/EUR", "DOGE/EUR", "SUI/EUR", "NEAR/EUR", "RENDER/EUR",
                "ADA/EUR", "DOT/EUR",
            ]
            tickers = exchange.fetch_tickers(symbols)
            cash = float(balance["total"].get("EUR", 0))
            total = cash
            pos_list = []
            for sym in symbols:
                coin = sym.split("/")[0]
                qty = float(balance["total"].get(coin, 0))
                t = tickers.get(sym)
                if t and t.get("last"):
                    val = qty * t["last"]
                    if val > 1.0:
                        total += val
                        pos_list.append({
                            "coin": coin, "qty": round(qty, 4),
                            "price": round(t["last"], 4),
                            "value": round(val, 2),
                        })
            report["live_balances"]["kraken"] = {
                "cash_eur": round(cash, 2),
                "total_eur": round(total, 2),
                "positions": pos_list,
            }
        except Exception as e:
            report["live_balances"]["kraken_error"] = str(e)
    else:
        report["live_balances"]["kraken"] = {"error": "Keys missing"}

    # ------------------------------------------------------------------
    # 5. Live Alpaca balance
    # ------------------------------------------------------------------
    alpaca_key = os.getenv("ALPACA_API_KEY")
    alpaca_secret = os.getenv("ALPACA_SECRET_KEY")
    alpaca_base = "https://api.alpaca.markets"
    if alpaca_key and alpaca_secret:
        try:
            headers = {
                "APCA-API-KEY-ID": alpaca_key,
                "APCA-API-SECRET-KEY": alpaca_secret,
                "Content-Type": "application/json",
            }
            acc = requests.get(
                f"{alpaca_base}/v2/account", headers=headers, timeout=10).json()
            pos = requests.get(
                f"{alpaca_base}/v2/positions", headers=headers, timeout=10).json()
            report["live_balances"]["alpaca"] = {
                "cash": acc.get("cash"),
                "equity": acc.get("equity"),
                "buying_power": acc.get("buying_power"),
                "positions": pos if isinstance(pos, list) else [],
            }
        except Exception as e:
            report["live_balances"]["alpaca_error"] = str(e)
    else:
        report["live_balances"]["alpaca"] = {"error": "Keys missing"}

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    balance_errors = []
    for name, val in report["live_balances"].items():
        if name.endswith("_error"):
            balance_errors.append(f"{name}: {val}")
        elif isinstance(val, dict) and val.get("error"):
            balance_errors.append(f"{name}: {val['error']}")
    for err in balance_errors:
        report["summary"]["warnings"].append(f"Live balance API error — {err}")

    has_warnings = len(report["summary"]["warnings"]) > 0
    has_errors = bool(balance_errors) or any(
        v.get("ok") is False
        for section in ["database", "kraken_strategies", "alpaca_strategies"]
        for k, v in report.get(section, {}).items()
        if isinstance(v, dict)
    )
    report["summary"]["healthy"] = not has_warnings and not has_errors

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
