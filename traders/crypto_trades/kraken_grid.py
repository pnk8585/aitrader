"""kraken_grid.py — Grid trading strategy on Kraken.

Standalone grid engine that profits from sideways/ranging markets.
Uses LIMIT orders for maker-fee advantage, falls back to market after 2 cycles.
Own exchange_name='kraken-grid' (paper: 'paper-kraken-grid'), own grid_state table.
"""

import os
import sys

_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import fcntl
import ccxt
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "extreme"))
from db_prices import get_connection, close_connection, base_symbol, insert_prices, log_trade as db_log_trade
from traders.common.config import ensure_log_dir
from traders.strategies.grid import config as GC
from traders.strategies.grid.engine import create_grid, load_grid, save_grid, run_cycle
from traders.strategies.regime.router import should_enter

ensure_log_dir()

KRAKEN_API_KEY = os.getenv("KRAKEN_API_KEY")
KRAKEN_SECRET = os.getenv("KRAKEN_SECRET")
if not KRAKEN_API_KEY or not KRAKEN_SECRET:
    print("Error: Missing Kraken credentials in .env", file=sys.stderr)
    sys.exit(1)

exchange = ccxt.kraken({
    "apiKey": KRAKEN_API_KEY,
    "secret": KRAKEN_SECRET,
    "enableRateLimit": True,
})

EXCHANGE_NAME = GC.EXCHANGE_NAME
IS_PAPER = os.environ.get("AITRADER_MODE") == "paper"
if IS_PAPER:
    EXCHANGE_NAME = f"paper-{EXCHANGE_NAME}"


def log_trade(db_conn, action, ticker, price, qty, value_eur, reason, **kwargs):
    db_log_trade(
        db_conn, EXCHANGE_NAME,
        action=action, ticker=ticker, signal_strength="GRID",
        momentum_pct=0.0, entry_price=price, current_price=price,
        unrealized_plpc=0.0, order_id=None, quantity=qty,
        estimated_value=round(value_eur, 2), position_size_pct=0.0,
        portfolio_equity=0.0, reason=reason,
        **kwargs,
    )


def run_cycle():
    db_conn = get_connection()

    try:
        tickers = exchange.fetch_tickers(GC.CRYPTO_PAIRS) or {}
        price_map = {
            base_symbol(sym): tickers[sym]["last"]
            for sym in GC.CRYPTO_PAIRS
            if tickers.get(sym) and tickers[sym].get("last") is not None
        }
        insert_prices(db_conn, price_map)
        balance = (exchange.fetch_balance() or {}).get("total") or {}
        cash_eur = float(balance.get("EUR", 0.0))
    except Exception as e:
        print(f"Grid: market data fetch failed: {e}", file=sys.stderr)
        close_connection(db_conn)
        return

    notify_lines = []
    active_grids = 0

    # query capital already committed to active grids
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(capital_allocated), 0) FROM grid_state "
                "WHERE status = 'active' AND exchange = %s",
                (EXCHANGE_NAME,),
            )
            existing_allocated = float(cur.fetchone()[0])
    except Exception:
        existing_allocated = 0.0

    for pair in GC.CRYPTO_PAIRS:
        if active_grids >= GC.MAX_OPEN_GRIDS:
            break

        grid = load_grid(db_conn, pair, EXCHANGE_NAME)

        if grid is None:
            allowed, reason = should_enter(db_conn, base_symbol(pair), "grid")
            if not allowed:
                report_line = f"⏭️ {pair}: {reason}"
                print(report_line)
                continue
            if cash_eur < GC.MIN_TRADE_EUR * GC.NUM_GRIDS:
                continue
            available = cash_eur - existing_allocated
            if available < GC.MIN_TRADE_EUR * GC.NUM_GRIDS:
                continue
            grid = create_grid(db_conn, pair, cash_eur, available_cash=available)
            if grid is None:
                continue
            existing_allocated += grid["capital_allocated"]
            save_grid(db_conn, grid, EXCHANGE_NAME)
            notify_lines.append(
                f"🔲 Grid created: {pair} €{grid['grid_low']}-€{grid['grid_high']}, "
                f"{grid['num_grids']} levels, €{round(grid['capital_allocated'], 2)} allocated"
            )
            print(notify_lines[-1])
            active_grids += 1
            continue

        if grid["status"] != "active":
            if grid["status"] == "paused":
                active_grids += 1
            continue

        active_grids += 1
        grid, report = run_cycle(db_conn, exchange, pair, grid, IS_PAPER)
        save_grid(db_conn, grid, EXCHANGE_NAME)

        for action in report:
            print(action)
            if action.startswith("💰"):
                notify_lines.append(action)
                for trade in grid.pop("_cycle_trades", []):
                    log_trade(db_conn, "SELL", pair, trade["price"], trade["qty"],
                              trade["pnl"], f"Grid cycle: {pair}")
            elif action.startswith("🛑"):
                notify_lines.append(action)
            elif action.startswith("📏"):
                notify_lines.append(action)
            elif action.startswith("🔲"):
                notify_lines.append(action)

    close_connection(db_conn)

    if notify_lines:
        try:
            from app.notify import send_telegram
            send_telegram("\n".join(notify_lines))
        except Exception:
            pass


def main():
    lock_fp = open(GC.LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another kraken_grid cycle is already running — skipping this tick.",
              file=sys.stderr)
        return
    try:
        run_cycle()
    except Exception as e:
        import traceback
        print(f"Kraken grid CRASHED: {e}\n{traceback.format_exc()}", file=sys.stderr)
        try:
            from app.notify import send_telegram
            send_telegram(f"🚨 Kraken grid crashed: {e}")
        except Exception:
            pass
    finally:
        try:
            fcntl.flock(lock_fp, fcntl.LOCK_UN)
            lock_fp.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
