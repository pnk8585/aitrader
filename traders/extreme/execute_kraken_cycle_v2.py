"""
execute_kraken_cycle_v2.py — Kraken crypto strategy, redesigned.

WHY v2 EXISTS
-------------
v1 (execute_kraken_cycle.py) lost ~23% of capital in 2 days. Forensics on the
trade_log (see .ai/kraken-strategy-postmortem-and-v2.md):
  - 27 round-trips, NET avg -0.78%/trip, only 19% cleared the 0.52% fee.
  - Avg winner +0.50% gross < 0.52% round-trip fee  => negative by construction.
  - corr(entry momentum, forward return) = +0.08  => momentum was pure noise.
  - Median hold 40 min, ~13 trips/day  => death by fees (~14% burned on fees).
  - Every exit was a flat "stale rotation" / "breakeven" sell = a fee loss.

v2 PRIME DIRECTIVE: trade RARELY, only pullbacks inside a higher-timeframe
uptrend, let winners run far past the fee wall, cut losers fast, and NEVER sell
flat. The single biggest lever is trade frequency: going from ~13 to ~2
trips/day removes ~5.7%/day of pure fee drag.

Drop-in replacement for execute_kraken_cycle.py: same DB schema, same CCXT /
Kraken plumbing, same "Kraken is the sole price writer" responsibility.
"""

import os
import sys
import json
import time
import ccxt
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from db_prices import (get_connection, insert_prices, get_one_hour_momentum,
                       close_connection, base_symbol,
                       load_trading_state, save_trading_state,
                       load_notify_state, save_notify_state as db_save_notify_state,
                       log_trade as db_log_trade)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
env_path = "PROJECT_ROOT/.env"
load_dotenv(dotenv_path=env_path)

KRAKEN_API_KEY = os.getenv("KRAKEN_API_KEY")
KRAKEN_SECRET = os.getenv("KRAKEN_SECRET")

if not KRAKEN_API_KEY or not KRAKEN_SECRET:
    print("Error: Missing Kraken credentials in .env", file=sys.stderr)
    sys.exit(1)

exchange = ccxt.kraken({
    'apiKey': KRAKEN_API_KEY,
    'secret': KRAKEN_SECRET,
    'enableRateLimit': True,
})

EXCHANGE_NAME = "kraken"
LOG_DIR = "PROJECT_ROOT/logs"
os.makedirs(LOG_DIR, exist_ok=True)

# Full candidate pool. The volatility filter below decides which are tradeable
# on any given cycle, so quiet majors (BTC/ETH/XRP/...) self-exclude most days.
CRYPTO_PAIRS = ["BTC/EUR", "ETH/EUR", "SOL/EUR", "AVAX/EUR", "LINK/EUR",
                "XRP/EUR", "DOGE/EUR", "SUI/EUR", "NEAR/EUR", "RENDER/EUR",
                "ADA/EUR", "DOT/EUR"]

# --- Fees -----------------------------------------------------------------
# Kraken taker ~0.26%/side => 0.52% round-trip. Everything below is sized so
# the expected winner is several multiples of this number.
ROUND_TRIP_FEE_PCT = 0.52

# --- Universe / regime filters --------------------------------------------
VOL_FLOOR_PCT = 3.0        # require >=3.0% hi-lo range so a -2% stop sits below the noise
VOL_WINDOW_MIN = 360       # 6h volatility window
TREND_3H_MIN_PCT = 1.0     # price must be >= +1.0% vs 3h ago (uptrend)
TREND_3H_MIN = 180         # minutes
TREND_6H_MIN = 360         # price must also be > price 6h ago

# --- Entry (buy the dip inside the uptrend, never the blow-off top) -------
PULLBACK_MIN_PCT = 0.5     # current price must be >=0.5% below the last-1h high
BLOWOFF_GUARD_1H_PCT = 4.0 # skip if 1h momentum > +4% (that's the top, it reverts)

# --- Exits ----------------------------------------------------------------
MIN_HARD_STOP_PCT = 2.0     # stop floor (dynamic: max(2%, 0.5 × range6h) per position)
TRAIL_ARM_PCT = 1.2        # arm trailing TP earlier (gives more net after giveback)
TRAIL_GIVEBACK_PCT = 0.5   # once armed, exit on 0.5% giveback from peak
HARD_TP_CAP_PCT = 6.0      # absolute take-profit ceiling
MAX_HOLD_HOURS = 12.0      # only force-exit a *dead* (net-neg, trend-broken) bag

# --- Position sizing / risk -----------------------------------------------
DEPLOY_FRACTION = 0.97     # deploy ~97% of cash into the single best setup
MIN_TRADE_EUR = 0.45       # Kraken minimum
MAX_OPEN_SMALL = 1         # one position at a time for a small account
MAX_OPEN_LARGE = 2         # allow 2 only above EQUITY_TWO_POS
EQUITY_TWO_POS = 400.0

COOLDOWN_MIN = 90          # per-coin cooldown after any exit (kills churn)
MAX_TRADES_PER_DAY = 4     # hard overtrading cap
DAILY_LOSS_BREAKER_PCT = -4.0  # stop trading for the UTC day past this realized loss


# ---------------------------------------------------------------------------
# Local SQL helpers (read-only; asset_prices is written by the cycle itself)
# ---------------------------------------------------------------------------
def get_momentum_over(conn, symbol, minutes):
    """% change of latest price vs the price ~`minutes` ago.

    Looks in a +-15% window around the target age so a missing exact sample
    doesn't break the read. Returns None if there isn't enough history yet.
    """
    if conn is None:
        return None
    base = base_symbol(symbol)
    lo = int(minutes * 1.15)
    hi = int(minutes * 0.85)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT price FROM asset_prices WHERE exchange=%s AND symbol=%s "
                "ORDER BY timestamp DESC LIMIT 1", (EXCHANGE_NAME, base))
            latest = cur.fetchone()
            if not latest or latest[0] is None:
                return None
            latest_price = float(latest[0])
            cur.execute(
                "SELECT price FROM asset_prices WHERE exchange=%s AND symbol=%s "
                "AND timestamp <= CURRENT_TIMESTAMP - make_interval(mins => %s) "
                "AND timestamp >= CURRENT_TIMESTAMP - make_interval(mins => %s) "
                "ORDER BY timestamp DESC LIMIT 1",
                (EXCHANGE_NAME, base, hi, lo))
            past = cur.fetchone()
            if not past or past[0] is None:
                return None
            past_price = float(past[0])
    except Exception as e:
        print(f"get_momentum_over failed: {e}", file=sys.stderr)
        return None
    if past_price == 0:
        return None
    return (latest_price - past_price) / past_price * 100.0


def get_range_pct(conn, symbol, minutes):
    """Hi-lo range (%) over the last `minutes`. None if too little history."""
    if conn is None:
        return None
    base = base_symbol(symbol)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT MIN(price), MAX(price), COUNT(*) FROM asset_prices "
                "WHERE exchange=%s AND symbol=%s "
                "AND timestamp >= CURRENT_TIMESTAMP - make_interval(mins => %s)",
                (EXCHANGE_NAME, base, minutes))
            row = cur.fetchone()
    except Exception as e:
        print(f"get_range_pct failed: {e}", file=sys.stderr)
        return None
    if not row or row[0] is None or row[2] < 6:  # need a handful of samples
        return None
    lo, hi = float(row[0]), float(row[1])
    if lo == 0:
        return None
    return (hi - lo) / lo * 100.0


def get_recent_high(conn, symbol, minutes):
    """Highest price over the last `minutes`. None if no history."""
    if conn is None:
        return None
    base = base_symbol(symbol)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT MAX(price) FROM asset_prices WHERE exchange=%s AND symbol=%s "
                "AND timestamp >= CURRENT_TIMESTAMP - make_interval(mins => %s)",
                (EXCHANGE_NAME, base, minutes))
            row = cur.fetchone()
    except Exception as e:
        print(f"get_recent_high failed: {e}", file=sys.stderr)
        return None
    if not row or row[0] is None:
        return None
    return float(row[0])


def get_recent_low(conn, symbol, minutes):
    """Lowest price over the last `minutes`. None if no history."""
    if conn is None:
        return None
    base = base_symbol(symbol)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT MIN(price) FROM asset_prices WHERE exchange=%s AND symbol=%s "
                "AND timestamp >= CURRENT_TIMESTAMP - make_interval(mins => %s)",
                (EXCHANGE_NAME, base, minutes))
            row = cur.fetchone()
    except Exception as e:
        print(f"get_recent_low failed: {e}", file=sys.stderr)
        return None
    if not row or row[0] is None:
        return None
    return float(row[0])


def last_exit_time(conn, symbol):
    """Timestamp of the most recent SELL for this coin, for cooldown. None if never."""
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT MAX(timestamp) FROM trade_log WHERE exchange=%s "
                "AND ticker=%s AND action='SELL'", (EXCHANGE_NAME, symbol))
            row = cur.fetchone()
    except Exception as e:
        print(f"last_exit_time failed: {e}", file=sys.stderr)
        return None
    return row[0] if row else None


def trades_today(conn):
    """Count of BUYs since 00:00 UTC, for the daily overtrading cap."""
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


def realized_pnl_today_pct(conn):
    """Sum of today's SELL unrealized_plpc (stored as a fraction) as a %.

    Approximates realized PnL for the daily loss circuit-breaker. Net of fee is
    not stored, so we subtract the round-trip fee per closed trade.
    """
    if conn is None:
        return 0.0
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(unrealized_plpc),0), COUNT(*) FROM trade_log "
                "WHERE exchange=%s AND action='SELL' "
                "AND timestamp >= date_trunc('day', CURRENT_TIMESTAMP AT TIME ZONE 'UTC')",
                (EXCHANGE_NAME,))
            row = cur.fetchone()
    except Exception as e:
        print(f"realized_pnl_today_pct failed: {e}", file=sys.stderr)
        return 0.0
    gross_pct = float(row[0]) * 100.0
    n = int(row[1])
    return gross_pct - n * ROUND_TRIP_FEE_PCT


# ---------------------------------------------------------------------------
# Trade logging shim (same signature shape as v1)
# ---------------------------------------------------------------------------
def log_trade(db_conn, action, ticker, signal_strength, momentum_pct, entry_price,
              current_price, unrealized_plpc, order_id, quantity,
              estimated_value_eur, position_size_pct, portfolio_equity, reason):
    db_log_trade(
        db_conn, EXCHANGE_NAME,
        action=action, ticker=ticker, signal_strength=signal_strength,
        momentum_pct=round(momentum_pct, 4) if momentum_pct else 0.0,
        entry_price=round(entry_price, 4) if entry_price else 0.0,
        current_price=round(current_price, 4) if current_price else 0.0,
        unrealized_plpc=round(unrealized_plpc, 5) if unrealized_plpc else 0.0,
        order_id=order_id,
        quantity=round(quantity, 6) if quantity else 0.0,
        estimated_value=round(estimated_value_eur, 2) if estimated_value_eur else 0.0,
        position_size_pct=round(position_size_pct, 4) if position_size_pct else 0.0,
        portfolio_equity=round(portfolio_equity, 2) if portfolio_equity else 0.0,
        reason=reason,
    )


def get_entry_price_and_time(symbol, current_price):
    """Recover entry price/time from Kraken fills when local state is missing."""
    try:
        trades = exchange.fetch_my_trades(symbol, limit=10)
        buy_trades = [t for t in trades if t['side'] == 'buy']
        if buy_trades:
            buy_trades = sorted(buy_trades, key=lambda x: x['timestamp'], reverse=True)
            latest = buy_trades[0]
            return float(latest['price']), latest['datetime']
    except Exception as e:
        print(f"Error fetching trades for {symbol}: {e}", file=sys.stderr)
    return current_price, datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Main cycle
# ---------------------------------------------------------------------------
def run_cycle():
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "success",
        "strategy": "v2-pullback-in-uptrend",
        "positions_managed": [],
        "scanned_assets": [],
        "action_taken": "NONE",
        "details": "",
    }

    db_conn = get_connection()
    state = load_trading_state(db_conn, EXCHANGE_NAME)
    notify_state = load_notify_state(db_conn, EXCHANGE_NAME)
    should_notify = False
    msg_lines = []

    # Heartbeat emitter, called on every exit path so hourly updates never skip.
    def finalize():
        nonlocal should_notify
        last_str = notify_state.get("last_notify_time", "1970-01-01T00:00:00Z")
        last_time = datetime.fromisoformat(last_str.replace("Z", "+00:00"))
        now_utc = datetime.now(timezone.utc)
        if (now_utc - last_time).total_seconds() >= 3600.0:
            should_notify = True
            msg_lines.insert(0, "⏱️ **Kraken v2 Hourly Update:**")
        if should_notify:
            pos_lines = []
            for p in positions:
                sym = p["symbol"]
                ss = new_state.get(sym, {})
                ent = ss.get("entry_price", p["current_price"])
                cur_p = p["current_price"]
                pl = (cur_p - ent) / ent * 100.0 if ent else 0.0
                peak = ss.get("peak_plpc", 0.0)
                pos_lines.append(f"📈 **{sym} (Kraken v2)**: {round(pl, 2)}% (Peak: +{round(peak, 2)}%)")
            msg_lines.extend(pos_lines or ["🔍 Καμία ανοιχτή θέση στο Kraken (100% Cash)."])
            print("\n".join(msg_lines))
            notify_state["last_notify_time"] = now_utc.isoformat().replace("+00:00", "Z")
        db_save_notify_state(db_conn, EXCHANGE_NAME, notify_state)
        close_connection(db_conn)

    # 1. Fetch tickers + balances. Persist prices FIRST (sole price writer).
    try:
        tickers = exchange.fetch_tickers(CRYPTO_PAIRS)
        price_map = {
            base_symbol(sym): tickers[sym]['last']
            for sym in CRYPTO_PAIRS
            if tickers.get(sym) and tickers[sym].get('last') is not None
        }
        insert_prices(db_conn, price_map)
        balance = exchange.fetch_balance()
    except Exception as e:
        report["status"] = "error"
        report["details"] = f"Failed to fetch market/account data: {e}"
        print(json.dumps(report), file=sys.stderr)
        close_connection(db_conn)
        return

    cash_eur = balance['total'].get('EUR', 0.0)
    portfolio_value = cash_eur
    positions = []
    for sym in CRYPTO_PAIRS:
        coin = sym.split('/')[0]
        qty = balance['total'].get(coin, 0.0)
        ticker = tickers.get(sym)
        if not ticker:
            continue
        price = ticker['last']
        value_eur = qty * price
        if value_eur > 1.0:  # 1 EUR dust limit
            portfolio_value += value_eur
            positions.append({'symbol': sym, 'coin': coin, 'qty': qty,
                              'current_price': price, 'value_eur': value_eur})

    report["portfolio_equity"] = portfolio_value
    report["buying_power"] = cash_eur
    report["open_positions_count"] = len(positions)

    # Initialise local per-symbol state (entry price/time, peak).
    new_state = {}
    for pos in positions:
        sym = pos['symbol']
        new_state[sym] = state.get(sym, {})
        if "entry_price" not in new_state[sym]:
            ep, et = get_entry_price_and_time(sym, pos['current_price'])
            new_state[sym]["entry_price"] = ep
            new_state[sym]["entry_time"] = et
        new_state[sym].setdefault("peak_plpc", 0.0)

    # ---------------------------------------------------------------
    # 2. Manage open positions  (exit logic — NO flat/stale selling)
    # ---------------------------------------------------------------
    managed_any = False
    for pos in positions[:]:
        symbol = pos["symbol"]
        qty = pos["qty"]
        current_price = pos["current_price"]
        ss = new_state[symbol]
        entry_price = ss["entry_price"]
        entry_time = datetime.fromisoformat(ss["entry_time"].replace('Z', '+00:00'))
        now = datetime.now(timezone.utc)
        age_hours = (now - entry_time).total_seconds() / 3600.0

        unrealized_plpc = (current_price - entry_price) / entry_price * 100.0
        peak_plpc = max(unrealized_plpc, ss.get("peak_plpc", 0.0))
        ss["peak_plpc"] = peak_plpc
        new_state[symbol] = ss

        sell = False
        reason = ""

        # dynamic stop: at least MIN_HARD_STOP_PCT, but wider for volatile coins
        rng_6h = get_range_pct(db_conn, symbol, 360)
        effective_stop = -max(MIN_HARD_STOP_PCT, 0.5 * (rng_6h or MIN_HARD_STOP_PCT * 2))

        if unrealized_plpc <= effective_stop:
            sell, reason = True, f"Hard stop ({round(unrealized_plpc,2)}% <= {round(effective_stop,2)}%, rng6h={round(rng_6h or 0,2)}%)"
        elif unrealized_plpc >= HARD_TP_CAP_PCT:
            sell, reason = True, f"Take-profit cap (+{round(unrealized_plpc,2)}% >= +{HARD_TP_CAP_PCT}%)"
        elif peak_plpc >= TRAIL_ARM_PCT and unrealized_plpc <= (peak_plpc - TRAIL_GIVEBACK_PCT):
            sell, reason = True, (f"Trailing TP (peak +{round(peak_plpc,2)}% -> "
                                  f"+{round(unrealized_plpc,2)}%, net +{round(unrealized_plpc-ROUND_TRIP_FEE_PCT,2)}%)")
        elif age_hours >= MAX_HOLD_HOURS and unrealized_plpc < 0:
            # Only time-stop a DEAD bag: net-negative AND trend broken. Never a winner.
            trend_3h = get_momentum_over(db_conn, symbol, TREND_3H_MIN)
            if trend_3h is not None and trend_3h < 0:
                sell, reason = True, (f"Max-hold dead-bag exit ({round(age_hours,1)}h, "
                                      f"{round(unrealized_plpc,2)}%, 3h trend {round(trend_3h,2)}%)")

        pos_report = {"symbol": symbol, "unrealized_plpc": round(unrealized_plpc, 2),
                      "age_hours": round(age_hours, 2),
                      "action": "HOLD", "reason": reason or f"Hold (peak +{round(peak_plpc,2)}%)"}

        if sell:
            try:
                exchange.load_markets()
                fqty = float(exchange.amount_to_precision(symbol, qty))
                res = exchange.create_market_sell_order(symbol, fqty)
                pos_report["action"] = "SELL"
                managed_any = True
                should_notify = True
                msg_lines.append(f"🔄 **Πωλήθηκε {symbol} (Kraken v2)**: {reason}")
                log_trade(db_conn, action="SELL", ticker=symbol,
                          signal_strength="EXIT", momentum_pct=0.0,
                          entry_price=entry_price, current_price=current_price,
                          unrealized_plpc=unrealized_plpc / 100.0,
                          order_id=res.get("id"), quantity=qty,
                          estimated_value_eur=qty * current_price,
                          position_size_pct=0.0, portfolio_equity=portfolio_value,
                          reason=reason)
                new_state.pop(symbol, None)
                positions = [p for p in positions if p["symbol"] != symbol]
            except Exception as e:
                pos_report["action"] = "SELL_FAILED"
                pos_report["reason"] = f"Failed to sell: {e}"
                print(f"Error selling {symbol}: {e}", file=sys.stderr)

        report["positions_managed"].append(pos_report)

    save_trading_state(db_conn, EXCHANGE_NAME, new_state)
    if managed_any:
        try:
            balance = exchange.fetch_balance()
            cash_eur = balance['total'].get('EUR', 0.0)
        except Exception:
            pass

    # ---------------------------------------------------------------
    # 3. Risk gates before any buy
    # ---------------------------------------------------------------
    max_open = MAX_OPEN_LARGE if portfolio_value >= EQUITY_TWO_POS else MAX_OPEN_SMALL
    skip, skip_reason = False, ""
    if len(positions) >= max_open:
        skip, skip_reason = True, f"Max positions reached ({max_open})."
    elif cash_eur < MIN_TRADE_EUR:
        skip, skip_reason = True, "No buying power."
    elif trades_today(db_conn) >= MAX_TRADES_PER_DAY:
        skip, skip_reason = True, f"Daily trade cap reached ({MAX_TRADES_PER_DAY})."
    else:
        rpnl = realized_pnl_today_pct(db_conn)
        if rpnl <= DAILY_LOSS_BREAKER_PCT:
            skip, skip_reason = True, f"Daily loss breaker tripped ({round(rpnl,2)}% <= {DAILY_LOSS_BREAKER_PCT}%)."

    if skip:
        report["action_taken"] = "SKIP"
        report["details"] = skip_reason
        finalize()
        return

    # ---------------------------------------------------------------
    # 4. Entry scan: pullback inside a confirmed higher-TF uptrend
    # ---------------------------------------------------------------
    held = {p["symbol"] for p in positions}
    now = datetime.now(timezone.utc)
    candidates = []
    for sym in CRYPTO_PAIRS:
        if sym in held:
            continue
        ticker = tickers.get(sym)
        if not ticker or ticker.get('last') is None:
            continue
        price = ticker['last']

        # cooldown
        lx = last_exit_time(db_conn, sym)
        if lx is not None and (now - lx) < timedelta(minutes=COOLDOWN_MIN):
            continue

        # volatility floor — moves must be able to clear the fee
        rng = get_range_pct(db_conn, sym, VOL_WINDOW_MIN)
        if rng is None or rng < VOL_FLOOR_PCT:
            continue

        # higher-timeframe uptrend
        t3 = get_momentum_over(db_conn, sym, TREND_3H_MIN)
        t6 = get_momentum_over(db_conn, sym, TREND_6H_MIN)
        if t3 is None or t6 is None or t3 < TREND_3H_MIN_PCT or t6 <= 0:
            continue

        # anti blow-off top guard
        h1 = get_one_hour_momentum(db_conn, sym)
        if h1 is not None and h1 > BLOWOFF_GUARD_1H_PCT:
            continue

        # pullback: price must be at least PULLBACK_MIN_PCT below the 1h high
        hi1h = get_recent_high(db_conn, sym, 60)
        if hi1h is None or hi1h <= 0:
            continue
        pullback = (hi1h - price) / hi1h * 100.0
        if pullback < PULLBACK_MIN_PCT:
            continue

        # bounce gate: require price to be bouncing off lows,
        # not still falling (rejects falling knives)
        price_5m = get_momentum_over(db_conn, sym, 5)
        low15m = get_recent_low(db_conn, sym, 15)
        if (price_5m is not None and price_5m <= 0
                and low15m is not None and float(price) <= low15m * 1.001):  # type: ignore[operator]
            continue

        # quality score: strong trend, deep-ish pullback, healthy vol — but not a blow-off
        score = t3 + 0.5 * rng + pullback - max(0.0, (h1 or 0.0) - BLOWOFF_GUARD_1H_PCT)
        candidates.append({"symbol": sym, "price": price, "t3": t3, "t6": t6,
                           "rng": rng, "pullback": pullback, "h1": h1, "score": score})

    candidates.sort(key=lambda c: c["score"], reverse=True)
    report["scanned_assets"] = [
        {k: (round(v, 3) if isinstance(v, float) else v) for k, v in c.items()}
        for c in candidates[:8]
    ]

    if not candidates:
        report["action_taken"] = "SKIP"
        report["details"] = "No pullback-in-uptrend setup passed the filters."
        finalize()
        return

    best = candidates[0]
    symbol = best["symbol"]
    current_price = best["price"]

    # Sizing: one concentrated, well-chosen trade. Deploy ~97% of cash.
    order_size_eur = min(cash_eur * DEPLOY_FRACTION, cash_eur)
    if order_size_eur < MIN_TRADE_EUR:
        report["action_taken"] = "SKIP"
        report["details"] = "Order size below Kraken minimum."
        finalize()
        return

    qty = order_size_eur / current_price
    momentum_desc = (f"PULLBACK_IN_UPTREND (3h +{round(best['t3'],2)}%, "
                     f"dip -{round(best['pullback'],2)}%, vol {round(best['rng'],2)}%)")
    try:
        exchange.load_markets()
        fqty = float(exchange.amount_to_precision(symbol, qty))
        res = exchange.create_market_buy_order(symbol, fqty)
        report["action_taken"] = "BUY"
        report["details"] = f"Bought {symbol} for EUR {round(order_size_eur,2)}."
        should_notify = True
        msg_lines.append(f"🛒 **Αγοράστηκε {symbol} (Kraken v2)** "
                         f"(EUR {round(order_size_eur,2)} — {momentum_desc})")
        new_state[symbol] = {
            "entry_price": current_price,
            "entry_time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "peak_plpc": 0.0,
        }
        save_trading_state(db_conn, EXCHANGE_NAME, new_state)
        positions.append({"symbol": symbol, "current_price": current_price})
        log_trade(db_conn, action="BUY", ticker=symbol,
                  signal_strength="PULLBACK_IN_UPTREND",
                  momentum_pct=best["t3"], entry_price=current_price,
                  current_price=current_price, unrealized_plpc=0.0,
                  order_id=res.get("id"), quantity=fqty,
                  estimated_value_eur=order_size_eur,
                  position_size_pct=order_size_eur / portfolio_value * 100.0,
                  portfolio_equity=portfolio_value,
                  reason=f"{momentum_desc} on {symbol}. Deployed EUR {round(order_size_eur,2)}.")
    except Exception as e:
        report["action_taken"] = "BUY_FAILED"
        report["details"] = f"Failed to buy {symbol}: {e}"
        print(f"Buy failed: {e}", file=sys.stderr)

    finalize()


if __name__ == "__main__":
    run_cycle()
