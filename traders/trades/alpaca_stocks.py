"""alpaca_stocks.py — US stock momentum breakout on Alpaca.

Adapted from the crypto momentum script (execute_cycle.py):
  - Universe is US equities, not crypto.
  - Entry: daily >= 1.5% (vs previous close) OR intraday >= 1.0% (vs open).
  - Exit: trailing TP, profit lock, stop-loss (-3%), breakeven.
  - Hard cap of 3 entries per UTC day (self-imposed PDT protection).
  - Entries only fire while the market is open; exits also require an open
    market (stock orders can't fill when closed).
  - Stale rotation removed — not needed for a small stock book.
"""

import os
import sys

_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import json
import fcntl
import requests
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "extreme"))
from db_prices import (
                       DEBUG, get_connection, close_connection,
                       load_trading_state, save_trading_state,
                       load_notify_state, save_notify_state as db_save_notify_state,
                       log_trade as db_log_trade)
from traders.common.config import (
    ALPACA_BASE_URL, ALPACA_DATA_URL, DRY_RUN, ROOT_DIR, LOG_DIR, ensure_log_dir,
)
from traders.common.gates import check_gate, load_ai_gates
from traders.common.pnl_notify import format_sell_pnl_auto

ensure_log_dir()

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

for _k in ["ALPACA_API_KEY", "ALPACA_SECRET_KEY"]:
    if not os.environ.get(_k):
        print(f"Missing required env var: {_k}", file=sys.stderr)
        sys.exit(1)

# Headers for Alpaca
headers = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    "Content-Type": "application/json"
}

# US stock universe
STOCK_SYMBOLS = ["NVDA", "PLTR", "TSLA", "AMD", "GOOGL", "META", "AAPL", "MSFT", "AMZN", "AVGO"]


# Stock commissions are effectively ~0.005% per side (mostly $0 + tiny SEC/TAF
# fees) => ~0.01% round-trip. Stocks do NOT carry the ~0.5% crypto fee.
ROUND_TRIP_FEE_PCT = 0.01

# Stock ride-the-wave trailing stops (stocks less volatile than crypto)
TTP_PEAK_PCT = 3.0          # Trailing-Take-Profit: activate after +3.0% peak
TTP_GIVEBACK_PCT = 1.0      # Sell if we give back 1.0% from peak
PLOCK_PEAK_PCT = 5.0        # Profit-Lock: activate after +5.0% peak
PLOCK_FLOOR_PCT = 3.0       # Sell if we drop below +3.0% after hitting +5.0%
STOP_LOSS_PCT = -2.0        # Hard stop-loss
BREAKEVEN_PEAK_PCT = 1.0    # Breakeven protection arms after +1.0% peak
MAX_HOLD_HOURS = 8.0        # Hard time-stop (stocks mostly held intraday)

# --- Entry ----------------------------------------------------------------
DAILY_ENTRY_PCT = 2.0       # daily change (vs prev close) >= 2.0% qualifies
INTRADAY_ENTRY_PCT = 1.5    # OR intraday change (vs open) >= 1.5% qualifies

# --- Caps -----------------------------------------------------------------
MAX_TRADES_PER_DAY = 3      # hard cap on entries per UTC day (PDT protection)
MAX_OPEN_POSITIONS = 5

EXCHANGE_NAME = "alpaca-stocks"

# Paper mode: prefix + skip real API calls
_ALPACA_PAPER = os.environ.get("AITRADER_MODE") == "paper"
if _ALPACA_PAPER:
    EXCHANGE_NAME = "paper-alpaca-stocks"

# --- Concurrency guard ------------------------------------------------------
# Overlapping crons would double entries and race trading_state. A single flock
# makes any second concurrent run exit.
LOCK_FILE = os.path.join(LOG_DIR, "alpaca_stocks.lock")

def log_trade(db_conn, action, ticker, signal_strength, momentum_pct, entry_price,
              current_price, unrealized_plpc, order_id, client_order_id, quantity,
              estimated_value_usd, position_size_pct, portfolio_equity, reason,
              **kwargs):
    db_log_trade(
        db_conn, EXCHANGE_NAME,
        action=action, ticker=ticker, signal_strength=signal_strength,
        momentum_pct=round(momentum_pct, 4) if momentum_pct else 0.0,
        entry_price=round(entry_price, 4) if entry_price else 0.0,
        current_price=round(current_price, 4) if current_price else 0.0,
        unrealized_plpc=round(unrealized_plpc, 5) if unrealized_plpc else 0.0,
        order_id=order_id, client_order_id=client_order_id,
        quantity=round(quantity, 6) if quantity else 0.0,
        estimated_value=round(estimated_value_usd, 2) if estimated_value_usd else 0.0,
        position_size_pct=round(position_size_pct, 4) if position_size_pct else 0.0,
        portfolio_equity=round(portfolio_equity, 2) if portfolio_equity else 0.0,
        reason=reason,
        **kwargs,
    )


def trades_today(conn):
    """Count of BUYs since 00:00 UTC for this strategy, for the daily entry cap."""
    if conn is None:
        return 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM trade_log WHERE exchange=%s AND action='BUY' "
                "AND timestamp >= date_trunc('day', CURRENT_TIMESTAMP AT TIME ZONE 'UTC')",
                (EXCHANGE_NAME,))
            return int(cur.fetchone()[0])
    except Exception as e:
        print(f"trades_today failed: {e}", file=sys.stderr)
        return 0


def get_position_age_hours(symbol):
    try:
        url = f"{ALPACA_BASE_URL}/v2/orders?status=filled&symbols={symbol}&limit=10"
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            orders = response.json()
            buy_orders = [o for o in orders if o['side'] == 'buy']
            if buy_orders:
                filled_at_str = buy_orders[0].get('filled_at')
                if filled_at_str:
                    filled_at = datetime.fromisoformat(filled_at_str.replace('Z', '+00:00'))
                    now = datetime.now(timezone.utc)
                    age_seconds = (now - filled_at).total_seconds()
                    return age_seconds / 3600.0
    except Exception as e:
        print(f"Error checking position age for {symbol}: {e}", file=sys.stderr)
    return 0.0


def run_cycle():
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "success",
        "strategy": "stock-momentum",
        "positions_managed": [],
        "scanned_assets": [],
        "action_taken": "NONE",
        "details": ""
    }

    db_conn = get_connection()

    # Load state & notify state
    state = load_trading_state(db_conn, EXCHANGE_NAME)
    notify_state = load_notify_state(db_conn, EXCHANGE_NAME)
    should_notify = False
    msg_lines = []

    def finalize():
        nonlocal should_notify
        action = report.get("action_taken", "")
        is_error = report.get("status") == "error"
        has_trade = action in ("BUY", "SELL")
        has_fail = action in ("BUY_FAILED", "SELL_FAILED")

        if is_error:
            print(f"❌ Alpaca stocks: {report.get('details', 'unknown error')}")
        elif has_trade:
            for line in msg_lines:
                if line.startswith(("🛒", "🔄")):
                    print(line)
                    break
        elif has_fail:
            print(f"⚠️ Alpaca stocks {action}: {report.get('details', '')}")
        # else silent — heartbeat moved to hourly combined report

        db_save_notify_state(db_conn, EXCHANGE_NAME, notify_state)
        close_connection(db_conn)

    # 1. Fetch Account Info
    acc_url = f"{ALPACA_BASE_URL}/v2/account"
    acc_res = requests.get(acc_url, headers=headers, timeout=10)
    if acc_res.status_code != 200:
        report["status"] = "error"
        report["details"] = f"Failed to fetch account info: {acc_res.text}"
        finalize()
        return

    account = acc_res.json()
    equity = float(account.get("equity", 0.0))
    buying_power = float(account.get("buying_power", 0.0))

    # 2. Fetch Clock Info — stock orders only fill while the market is open.
    clock_url = f"{ALPACA_BASE_URL}/v2/clock"
    clock_res = requests.get(clock_url, headers=headers, timeout=10)
    market_open = False
    if clock_res.status_code == 200:
        market_open = clock_res.json().get("is_open", False)

    # 3. Fetch Positions
    pos_url = f"{ALPACA_BASE_URL}/v2/positions"
    pos_res = requests.get(pos_url, headers=headers, timeout=10)
    positions = []
    if pos_res.status_code == 200:
        positions = pos_res.json()

    report["portfolio_equity"] = equity
    report["buying_power"] = buying_power
    report["open_positions_count"] = len(positions)
    report["market_open"] = market_open

    # Prepare peaks state
    new_state = {}
    active_symbols = [p["symbol"] for p in positions]
    for sym in active_symbols:
        new_state[sym] = state.get(sym, {"peak_plpc": 0.0})

    # 4. Manage Open Positions
    managed_any = False

    for pos in positions:
        symbol = pos["symbol"]
        unrealized_plpc = float(pos["unrealized_plpc"]) * 100.0
        qty = float(pos["qty"])
        entry_price = float(pos["avg_entry_price"])
        current_price = float(pos["current_price"])

        age_hours = get_position_age_hours(symbol)

        # Track peak P&L and persist live position data so the row survives
        # the ON CONFLICT upsert in save_trading_state.
        sym_state = new_state.get(symbol, {"peak_plpc": 0.0})
        sym_state["entry_price"] = entry_price
        sym_state["quantity"] = qty
        if not sym_state.get("entry_time"):
            sym_state["entry_time"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        peak_plpc = max(unrealized_plpc, sym_state.get("peak_plpc", 0.0))
        sym_state["peak_plpc"] = peak_plpc
        new_state[symbol] = sym_state

        sell_triggered = False
        sell_reason = ""

        # Trailing Take Profit
        if peak_plpc >= TTP_PEAK_PCT and unrealized_plpc <= (peak_plpc - TTP_GIVEBACK_PCT):
            sell_triggered = True
            sell_reason = f"Trailing Take Profit hit (Peak: +{round(peak_plpc, 2)}% | Sold at: +{round(unrealized_plpc, 2)}%)"
        # Profit Lock
        elif peak_plpc >= PLOCK_PEAK_PCT and unrealized_plpc < PLOCK_FLOOR_PCT:
            sell_triggered = True
            sell_reason = f"Profit lock protection (Peak: +{round(peak_plpc, 2)}% | Sold at: +{round(unrealized_plpc, 2)}%)"
        # Stop-loss
        elif unrealized_plpc <= STOP_LOSS_PCT:
            sell_triggered = True
            sell_reason = f"Stop-loss hit ({round(unrealized_plpc, 2)}% <= {STOP_LOSS_PCT}%)"
        # Breakeven Protection (exit above fee floor so we lock NET-positive)
        elif peak_plpc >= BREAKEVEN_PEAK_PCT and unrealized_plpc <= ROUND_TRIP_FEE_PCT:
            sell_triggered = True
            sell_reason = f"Breakeven protection (Peak was +{round(peak_plpc, 2)}% | Current: +{round(unrealized_plpc, 2)}% | fee floor +{ROUND_TRIP_FEE_PCT}%)"
        # Hard max-hold time-stop
        elif age_hours >= MAX_HOLD_HOURS and unrealized_plpc <= ROUND_TRIP_FEE_PCT:
            sell_triggered = True
            sell_reason = f"Max-hold time-stop ({round(age_hours, 1)}h)"

        pos_report = {
            "symbol": symbol,
            "unrealized_plpc": unrealized_plpc,
            "age_hours": age_hours,
            "action": "HOLD",
            "reason": f"Within limits (peak: +{round(peak_plpc, 2)}%)" if not sell_triggered else sell_reason
        }

        if sell_triggered:
            # Stock orders can only fill while the market is open.
            if market_open:
                close_url = f"{ALPACA_BASE_URL}/v2/positions/{symbol}"
                if DRY_RUN:
                    print(f"DRY_RUN: SELL {symbol}", file=sys.stderr)
                    close_status = 204
                    close_body = {}
                elif _ALPACA_PAPER:
                    print(f"PAPER: SELL {symbol}", file=sys.stderr)
                    close_status = 204
                    close_body = {}
                else:
                    close_res = requests.delete(close_url, headers=headers, timeout=10)
                    close_status = close_res.status_code
                    close_body = close_res.json() if close_res.text else {}
                if close_status in [200, 201, 204]:
                    pos_report["action"] = "SELL"
                    pos_report["reason"] = sell_reason
                    managed_any = True
                    should_notify = True
                    msg_lines.append(f"🔄 **Πωλήθηκε {symbol}**: {sell_reason}"
                                     f"{format_sell_pnl_auto(entry_price, current_price, qty, prices_in='USD')}")
                    log_trade(db_conn,
                        action="SELL",
                        ticker=symbol,
                        signal_strength="EXIT",
                        momentum_pct=0.0,
                        entry_price=entry_price,
                        current_price=current_price,
                        unrealized_plpc=float(pos["unrealized_plpc"]),
                        order_id=close_body.get("id") if close_body else None,
                        client_order_id=None,
                        quantity=qty,
                        estimated_value_usd=qty * current_price,
                        position_size_pct=0.0,
                        portfolio_equity=equity,
                        reason=sell_reason,
                        strategy_name=EXCHANGE_NAME,
                    )
                    if symbol in new_state:
                        del new_state[symbol]
                    positions = [p for p in positions if p["symbol"] != symbol]
                else:
                    pos_report["action"] = "SELL_FAILED"
                    pos_report["reason"] = f"Failed to close position: {close_res.text}"
            else:
                pos_report["reason"] = f"{sell_reason} (Pending - market closed)"

        report["positions_managed"].append(pos_report)

    # Save state
    save_trading_state(db_conn, EXCHANGE_NAME, new_state)

    # Refresh account and positions if we sold anything
    if managed_any:
        acc_res = requests.get(acc_url, headers=headers, timeout=10)
        if acc_res.status_code == 200:
            account = acc_res.json()
            buying_power = float(account.get("buying_power", 0.0))
            equity = float(account.get("equity", 0.0))
        pos_res = requests.get(pos_url, headers=headers, timeout=10)
        if pos_res.status_code == 200:
            positions = pos_res.json()

    # 5. Entry gates
    # AI gate FIRST — if the overseer paused entries, skip them. Exits in step 4
    # always run regardless, so a paused script can still cut losers.
    skip_buying = False
    paused, gate_msg = check_gate(db_conn)
    if paused:
        print(f"AI GATE: script paused — {gate_msg}", file=sys.stderr)
        report["action_taken"] = "SKIP"
        report["details"] = f"AI gate paused: {gate_msg}"
        skip_buying = True
    elif gate_msg:
        print(f"AI GATE auto-resumed: {gate_msg}", file=sys.stderr)
        msg_lines.append(f"✅ **AI Gate**: {gate_msg}")
        should_notify = True
    elif not market_open:
        report["action_taken"] = "SKIP"
        report["details"] = "Market closed — entries only fire during market hours."
        skip_buying = True
    elif len(positions) >= MAX_OPEN_POSITIONS:
        report["action_taken"] = "SKIP"
        report["details"] = f"Max positions limit reached ({MAX_OPEN_POSITIONS} open)."
        skip_buying = True
    elif buying_power <= 0:
        report["action_taken"] = "SKIP"
        report["details"] = "No buying power available."
        skip_buying = True
    elif trades_today(db_conn) >= MAX_TRADES_PER_DAY:
        report["action_taken"] = "SKIP"
        report["details"] = f"Daily entry cap reached ({MAX_TRADES_PER_DAY}/day)."
        skip_buying = True

    if not skip_buying:
        # --- STOCK MOMENTUM SCAN ---
        candidates = []

        symbols_str = ",".join(STOCK_SYMBOLS)
        snap_url = f"{ALPACA_DATA_URL}/v2/stocks/snapshots?symbols={symbols_str}"
        snap_res = requests.get(snap_url, headers=headers, timeout=10)
        if snap_res.status_code == 200:
            data = snap_res.json()
            for symbol, snap in data.items():
                if not snap:
                    continue
                if any(p["symbol"] == symbol for p in positions):
                    continue

                daily = snap.get("dailyBar") or {}
                prev_daily = snap.get("prevDailyBar") or {}
                latest_trade = snap.get("latestTrade") or {}

                # Current price: prefer the latest trade, fall back to daily close.
                current_price = float(latest_trade.get("p", 0.0)) or float(daily.get("c", 0.0))
                if current_price <= 0:
                    continue

                open_price = float(daily.get("o", 0.0))
                prev_close = float(prev_daily.get("c", 0.0))

                # Daily change vs previous close.
                daily_change_pct = None
                if prev_close > 0:
                    daily_change_pct = (current_price - prev_close) / prev_close * 100.0

                # Intraday change vs today's open.
                intraday_change_pct = None
                if open_price > 0:
                    intraday_change_pct = (current_price - open_price) / open_price * 100.0

                candidates.append({
                    "symbol": symbol,
                    "daily_change_pct": daily_change_pct,
                    "intraday_change_pct": intraday_change_pct,
                    "current_price": current_price,
                    "volume": float(daily.get("v", 0.0))
                })

        # Sort by the strongest of daily vs intraday momentum.
        def best_signal(c):
            d = c["daily_change_pct"] if c["daily_change_pct"] is not None else -999.0
            i = c["intraday_change_pct"] if c["intraday_change_pct"] is not None else -999.0
            return max(d, i)

        candidates = sorted(candidates, key=best_signal, reverse=True)
        report["scanned_assets"] = candidates[:10]

        # Candidate qualification: daily >= 1.5 OR intraday >= 1.0
        signals = [
            c for c in candidates
            if (c["daily_change_pct"] is not None and c["daily_change_pct"] >= DAILY_ENTRY_PCT)
            or (c["intraday_change_pct"] is not None and c["intraday_change_pct"] >= INTRADAY_ENTRY_PCT)
        ]

        if signals:
            best_candidate = signals[0]
            symbol = best_candidate["symbol"]
            daily_change_pct = best_candidate["daily_change_pct"]
            intraday_change_pct = best_candidate["intraday_change_pct"]
            current_price = best_candidate["current_price"]

            d = daily_change_pct if daily_change_pct is not None else -999.0
            i = intraday_change_pct if intraday_change_pct is not None else -999.0

            # Sizing and signal strength: evaluate daily & intraday tiers
            # independently, keep whichever yields the larger size.
            max_position_pct = float(os.getenv("MAX_POSITION_PCT", "0.50"))

            # Daily tiers
            if d >= 4.0:
                daily_strength, daily_mult = "EXTREME_MOMENTUM", 1.0
            elif d >= 2.5:
                daily_strength, daily_mult = "STRONG_MOMENTUM", 0.67
            elif d >= DAILY_ENTRY_PCT:
                daily_strength, daily_mult = "MODERATE_MOMENTUM", 0.33
            else:
                daily_strength, daily_mult = None, 0.0

            # Intraday tiers
            if i >= 2.5:
                intraday_strength, intraday_mult = "EXTREME_MOMENTUM", 1.0
            elif i >= 1.5:
                intraday_strength, intraday_mult = "STRONG_MOMENTUM", 0.67
            elif i >= INTRADAY_ENTRY_PCT:
                intraday_strength, intraday_mult = "MODERATE_MOMENTUM", 0.33
            else:
                intraday_strength, intraday_mult = None, 0.0

            # Choose the stronger of the two tiers.
            if intraday_mult > daily_mult:
                signal_strength = intraday_strength
                sizing_mult = intraday_mult
                intraday_is_primary = True
            else:
                signal_strength = daily_strength if daily_strength is not None else intraday_strength
                sizing_mult = daily_mult if daily_mult > 0.0 else intraday_mult
                intraday_is_primary = False

            order_size_usd = equity * max_position_pct * sizing_mult

            # Small Account Rule
            if equity < 200.0:
                order_size_usd = buying_power
                reason_rule = "small account rule (< $200 equity)"
            else:
                reason_rule = f"{signal_strength} level sizing"

            if order_size_usd > buying_power:
                order_size_usd = buying_power

            if order_size_usd >= 1.0:
                # ── LLM evaluation before buy ─────────────────────────────
                from traders.common.llm_review import review_trade

                alpaca_sigs = {
                    "daily_pct": round(daily_change_pct, 2) if daily_change_pct else 0,
                    "intraday_pct": round(intraday_change_pct, 2) if intraday_change_pct else 0,
                    "strength": signal_strength,
                    "mult": round(sizing_mult, 3),
                }
                try:
                    llm_result = review_trade(
                        symbol=symbol,
                        strategy="stocks-momentum",
                        signals=alpaca_sigs,
                        price=current_price,
                        score=(abs(intraday_change_pct or daily_change_pct or 0)) / 2,
                        portfolio_euro=equity * 0.92,  # EUR approx
                        available_euro=order_size_usd * 0.92,
                        open_positions=len(positions),
                    )
                except Exception as e:
                    print(f"LLM review failed: {e} — buying directly", file=sys.stderr)
                    llm_result = {"verdict": "REJECT", "reason": f"LLM unavailable: {e}", "confidence": 0}

                if llm_result["verdict"] != "APPROVE":
                    report["action_taken"] = "SKIP"
                    report["details"] = f"LLM rejected {symbol}: {llm_result['reason']}"
                    msg_lines.append(f"❌ LLM απέρριψε {symbol}: {llm_result['reason']}")
                    should_notify = True
                    finalize()
                    return

                msg_lines.append(
                    f"✅ LLM ενέκρινε {symbol} (conf {llm_result['confidence']}/10): {llm_result['reason']}"
                )
                should_notify = True
                # ── End LLM evaluation ────────────────────────────────────

                # Place BUY order
                timestamp_suffix = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                client_order_id = f"stocks-{symbol}-{timestamp_suffix}"

                order_data = {
                    "symbol": symbol,
                    "notional": str(round(order_size_usd, 2)),
                    "side": "buy",
                    "type": "market",
                    "time_in_force": "day",
                    "client_order_id": client_order_id
                }

                order_url = f"{ALPACA_BASE_URL}/v2/orders"
                if DRY_RUN:
                    print(f"DRY_RUN: BUY {symbol} ${round(order_size_usd, 2)}", file=sys.stderr)
                    order = {"id": f"dry-run-{client_order_id}", "status": "filled"}
                    order_res_status = 201
                elif _ALPACA_PAPER:
                    print(f"PAPER: BUY {symbol} ${round(order_size_usd, 2)}", file=sys.stderr)
                    oid = f"paper-{client_order_id}"
                    order = {"id": oid, "client_order_id": oid, "status": "filled"}
                    order_res_status = 201
                else:
                    order_res = requests.post(order_url, headers=headers, json=order_data, timeout=10)
                    order_res_status = order_res.status_code
                    order = order_res.json() if order_res_status in (200, 201) else {}

                if order_res_status in [200, 201]:
                    order = order_res.json()
                    report["action_taken"] = "BUY"
                    report["details"] = f"Successfully placed buy order for {symbol} of amount ${round(order_size_usd, 2)}."
                    report["order_id"] = order.get("id")
                    should_notify = True
                    positions.append({
                        "symbol": symbol,
                        "unrealized_plpc": 0.0,
                        "qty": order_size_usd / current_price,
                        "avg_entry_price": current_price,
                        "current_price": current_price
                    })

                    if intraday_is_primary:
                        momentum_desc = f"{signal_strength} (+{round(intraday_change_pct, 2)}% intraday)"
                        momentum_value = intraday_change_pct
                    else:
                        momentum_desc = f"{signal_strength} (+{round(daily_change_pct, 2)}% daily)"
                        momentum_value = daily_change_pct

                    msg_lines.append(f"🛒 **Αγοράστηκε {symbol}** (${round(order_size_usd, 2)} - {momentum_desc})")

                    log_trade(db_conn,
                        action="BUY",
                        ticker=symbol,
                        signal_strength=signal_strength,
                        momentum_pct=momentum_value,
                        entry_price=current_price,
                        current_price=current_price,
                        unrealized_plpc=0.0,
                        order_id=order.get("id"),
                        client_order_id=client_order_id,
                        quantity=order_size_usd / current_price,
                        estimated_value_usd=order_size_usd,
                        position_size_pct=order_size_usd / equity * 100.0,
                        portfolio_equity=equity,
                        reason=f"{momentum_desc} on {symbol}. Deployed ${round(order_size_usd, 2)} per {reason_rule}.",
                        strategy_name=EXCHANGE_NAME,
                    )
                else:
                    report["action_taken"] = "BUY_FAILED"
                    err = order_res.text if not DRY_RUN else "dry-run failed"
                    report["details"] = f"Failed to place buy order for {symbol}: {err}"
            else:
                report["action_taken"] = "SKIP"
                report["details"] = "Order size too small."
        else:
            report["action_taken"] = "SKIP"
            report["details"] = "No momentum signals found."

    finalize()


def main():
    # ── Orchestrator integration ──────────────────────────────────────
    # Run tracking handled by container cron_orchestrator (DB-driven)
    # ──────────────────────────────────────────────────────────────────

    # Single-instance lock: a second overlapping cron job would double orders
    # and race trading_state. Bail out if the lock is already held.
    lock_fp = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another alpaca_stocks instance is running — exiting.", file=sys.stderr)
        return
    try:
        run_cycle()
    except Exception as e:
        import traceback
        # Loud, single-line alert plus a JSON error report so the cron/notifier
        # wrapper surfaces a crash instead of failing silently.
        print(f"🚨 **Alpaca stocks CRASHED**: {e}")
        print(json.dumps({"status": "error", "strategy": "stock-momentum",
                          "details": f"CRASH: {e}"}), file=sys.stderr)
        print(f"ALERT: alpaca_stocks crashed: {e}\n{traceback.format_exc()}",
              file=sys.stderr)
    finally:
        try:
            fcntl.flock(lock_fp, fcntl.LOCK_UN)
            lock_fp.close()
        except Exception:
            pass

    # ── Orchestrator: schedule next run ──────────────────────────────
    # (no-op: next_run_at managed by container cron_orchestrator)


if __name__ == "__main__":
    main()
