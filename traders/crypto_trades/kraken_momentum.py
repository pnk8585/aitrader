"""kraken_momentum.py — Momentum breakout strategy on Kraken.

Port of the Alpaca crypto momentum logic (execute_cycle.py) to CCXT/Kraken.
Entry: daily >= 2% or hourly >= 1.5%.
Exit: trailing TP, profit lock, stop-loss, breakeven, stale rotation.

SHARED-WALLET NOTE
------------------
This script shares the Kraken EUR wallet with kraken_pullback.py. To avoid the
two strategies fighting over the same coin:
  - momentum only EXITS positions it recorded in its own trading_state
    (EXCHANGE_NAME = "kraken-momentum"); it never sells a coin the pullback
    script is holding.
  - momentum never ENTERS a coin already present in the Kraken balance (held by
    either strategy), and respects a global cap on total open positions.
Each strategy has its own lock file so a */5 cron overlap can't double-fire.
"""

import os
import sys
import json
import uuid
import time
import fcntl
import ccxt
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "extreme"))
from db_prices import (get_connection, insert_prices, get_one_hour_momentum,
                       close_connection, base_symbol,
                       load_trading_state, save_trading_state,
                       load_notify_state, save_notify_state as db_save_notify_state,
                       log_trade as db_log_trade)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ROOT_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))
env_path = os.path.join(ROOT_DIR, ".env")
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

EXCHANGE_NAME = "kraken-momentum"
LOG_DIR = os.path.join(ROOT_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# Same candidate pool as the pullback strategy.
CRYPTO_PAIRS = ["BTC/EUR", "ETH/EUR", "SOL/EUR", "AVAX/EUR", "LINK/EUR",
                "XRP/EUR", "DOGE/EUR", "SUI/EUR", "NEAR/EUR", "RENDER/EUR",
                "ADA/EUR", "DOT/EUR"]

# --- Fees -----------------------------------------------------------------
# Kraken taker ~0.26%/side => 0.52% round-trip.
ROUND_TRIP_FEE_PCT = 0.52

# --- Entry (momentum breakout) --------------------------------------------
DAILY_ENTRY_PCT = 2.0      # daily change >= 2.0% qualifies
HOURLY_ENTRY_PCT = 1.5     # OR hourly change >= 1.5% qualifies
DAILY_WINDOW_MIN = 1440    # 24h lookback for "daily" change from the price DB
# Rotation needs a stronger fresh signal than a plain entry.
ROT_DAILY_PCT = 2.5
ROT_HOURLY_PCT = 2.0

# --- Exits ----------------------------------------------------------------
TTP_PEAK_PCT = 3.0          # trailing take-profit arms after +3.0% peak
TTP_GIVEBACK_PCT = 1.0      # ...sell if we give back 1.0% from peak
PLOCK_PEAK_PCT = 5.0        # profit-lock arms after +5.0% peak
PLOCK_FLOOR_PCT = 3.0       # ...sell if we drop below +3.0%
STOP_LOSS_PCT = -2.5        # hard stop-loss
BREAKEVEN_PEAK_PCT = 2.0    # breakeven protection: peak armed >= +2.0%...
# ...exits at the fee floor (ROUND_TRIP_FEE_PCT) so we never round-trip a loss.
STALE_FLAT_HOURS = 0.75     # held >45min and still flat (<+1.0%) => rotation candidate
STALE_FLAT_PLPC = 1.0
STALE_MAX_HOURS = 1.5       # held >1.5h => rotation candidate regardless
MAX_HOLD_HOURS = 12.0       # hard time-stop

# --- Position sizing / risk -----------------------------------------------
DEPLOY_FRACTION = 0.60     # base fraction of cash to deploy on the best setup
RISK_PER_TRADE_PCT = 4.0   # volatility cap: never risk more than this if stopped
MIN_TRADE_EUR = 0.45       # Kraken minimum
MAX_OPEN_MOMENTUM = 2      # max positions this strategy holds at once
MAX_TOTAL_OPEN = 5         # global cap across BOTH Kraken strategies (shared wallet)

# --- Concurrency guard ------------------------------------------------------
LOCK_FILE = os.path.join(ROOT_DIR, "logs/kraken_momentum.lock")

# --- AI Gates ---------------------------------------------------------------
AI_GATE_FILE = os.path.join(ROOT_DIR, "ai_overseer/ai_gate.json")

COOLDOWN_MIN = 90          # per-coin cooldown after any exit
MAX_TRADES_PER_DAY = 4     # hard overtrading cap
DAILY_LOSS_BREAKER_PCT = -4.0
CONSULT_DEPLOY_FRACTION = 0.5
CONSULT_MIN_SCORE = 2.0


def load_ai_gates():
    """Read AI gate conditions set by ai_overseer. Returns dict with defaults."""
    default = {"script_paused": False, "consult_on_entry": False, "reason": None}
    if not os.path.exists(AI_GATE_FILE):
        return default
    try:
        with open(AI_GATE_FILE) as f:
            gates = json.load(f)
        return {**default, **gates}
    except (json.JSONDecodeError, IOError):
        return default


# ---------------------------------------------------------------------------
# Pending AI review — bot finds candidates, AI approves before buying
# ---------------------------------------------------------------------------
PENDING_REVIEW_FILE = os.path.join(ROOT_DIR, "ai_overseer/pending_review.json")
PENDING_LOCK_FILE = os.path.join(ROOT_DIR, "ai_overseer/.pending_review.lock")
PENDING_REVIEW_TIMEOUT_MIN = 120


def _with_pending_lock(func):
    """Execute func with exclusive flock on the pending lock file."""
    os.makedirs(os.path.dirname(PENDING_LOCK_FILE), exist_ok=True)
    with open(PENDING_LOCK_FILE, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            return func()
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def load_pending_review():
    """Read the pending review file. Returns dict with defaults if absent."""
    default = {"status": None, "bot": None, "symbol": None,
               "verdict": None, "verdict_reason": None,
               "created_at": None, "reviewed_at": None}
    if not os.path.exists(PENDING_REVIEW_FILE):
        return default
    try:
        with open(PENDING_REVIEW_FILE) as f:
            data = json.load(f)
        return {**default, **data}
    except (json.JSONDecodeError, IOError):
        return default


def write_pending_review(data):
    """Atomically write pending review data."""
    os.makedirs(os.path.dirname(PENDING_REVIEW_FILE), exist_ok=True)
    tmp = f"{PENDING_REVIEW_FILE}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, PENDING_REVIEW_FILE)


def clear_pending_review():
    """Remove the pending review file."""
    if os.path.exists(PENDING_REVIEW_FILE):
        os.remove(PENDING_REVIEW_FILE)


def _submit_candidate(pending_data):
    """Submit candidate under lock — re-checks that no other bot got there first."""
    def _do():
        if os.path.exists(PENDING_REVIEW_FILE):
            try:
                with open(PENDING_REVIEW_FILE) as f:
                    existing = json.load(f)
                if existing.get("status") == "pending":
                    return False
            except (json.JSONDecodeError, IOError):
                pass
        write_pending_review(pending_data)
        return True
    return _with_pending_lock(_do)


COOLDOWN_MIN = 90          # per-coin cooldown after any exit
# Local SQL helpers (read-only; asset_prices is written by this cycle)
# ---------------------------------------------------------------------------
def get_momentum_over(conn, symbol, minutes):
    """% change of latest price vs the price ~`minutes` ago.

    Reads against the shared "kraken" price feed (the pullback script and this
    one both write base prices under exchange='kraken' via insert_prices).
    Returns None if there isn't enough history yet.
    """
    if conn is None:
        return None
    base = base_symbol(symbol)
    lo = int(minutes * 1.15)
    hi = int(minutes * 0.85)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT price FROM asset_prices WHERE exchange='kraken' AND symbol=%s "
                "ORDER BY timestamp DESC LIMIT 1", (base,))
            latest = cur.fetchone()
            if not latest or latest[0] is None:
                return None
            latest_price = float(latest[0])
            cur.execute(
                "SELECT price FROM asset_prices WHERE exchange='kraken' AND symbol=%s "
                "AND timestamp <= CURRENT_TIMESTAMP - make_interval(mins => %s) "
                "AND timestamp >= CURRENT_TIMESTAMP - make_interval(mins => %s) "
                "ORDER BY timestamp DESC LIMIT 1",
                (base, hi, lo))
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
                "WHERE exchange='kraken' AND symbol=%s "
                "AND timestamp >= CURRENT_TIMESTAMP - make_interval(mins => %s)",
                (base, minutes))
            row = cur.fetchone()
    except Exception as e:
        print(f"get_range_pct failed: {e}", file=sys.stderr)
        return None
    if not row or row[0] is None or row[2] < 6:
        return None
    lo, hi = float(row[0]), float(row[1])
    if lo == 0:
        return None
    return (hi - lo) / lo * 100.0


def last_exit_time(conn, symbol):
    """Timestamp of the most recent SELL for this coin (this strategy), for cooldown."""
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
    """Count of BUYs since 00:00 UTC for this strategy, for the daily cap."""
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
    """Approx realized PnL today (%) for the daily loss circuit-breaker."""
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
# Trade logging shim
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


def extract_fill(res, fallback_price):
    """Recover the actual average fill price and filled qty from a CCXT order."""
    price, qty = None, None
    if isinstance(res, dict):
        for k in ("average", "price"):
            v = res.get(k)
            if v:
                price = float(v)
                break
        filled = res.get("filled")
        cost = res.get("cost")
        if filled:
            qty = float(filled)
        if price is None and cost and filled:
            price = float(cost) / float(filled)
    return (price or fallback_price), qty


def momentum_signal(daily, hourly):
    """Return (signal_strength, sizing_mult) for a candidate, or (None, 0.0).

    Evaluates daily and hourly tiers independently and keeps the larger size.
    """
    # Daily tiers
    if daily is not None and daily >= 5.0:
        d_str, d_mult = "EXTREME_MOMENTUM", 1.0
    elif daily is not None and daily >= 3.0:
        d_str, d_mult = "STRONG_MOMENTUM", 0.67
    elif daily is not None and daily >= DAILY_ENTRY_PCT:
        d_str, d_mult = "MODERATE_MOMENTUM", 0.33
    else:
        d_str, d_mult = None, 0.0
    # Hourly tiers
    if hourly is not None and hourly >= 3.0:
        h_str, h_mult = "EXTREME_MOMENTUM", 1.0
    elif hourly is not None and hourly >= 2.0:
        h_str, h_mult = "STRONG_MOMENTUM", 0.67
    elif hourly is not None and hourly >= HOURLY_ENTRY_PCT:
        h_str, h_mult = "MODERATE_MOMENTUM", 0.33
    else:
        h_str, h_mult = None, 0.0

    if h_mult > d_mult:
        return h_str, h_mult
    if d_str is not None:
        return d_str, d_mult
    return h_str, h_mult


# ---------------------------------------------------------------------------
# Main cycle
# ---------------------------------------------------------------------------
def run_cycle():
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "success",
        "strategy": "momentum-breakout",
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

    def finalize():
        nonlocal should_notify
        action = report.get("action_taken", "")
        is_error = report.get("status") == "error"
        has_trade = action in ("BUY", "SELL")
        has_fail = action in ("BUY_FAILED", "SELL_FAILED")

        last_str = notify_state.get("last_notify_time", "1970-01-01T00:00:00Z")
        last_time = datetime.fromisoformat(last_str.replace("Z", "+00:00"))
        now_utc = datetime.now(timezone.utc)
        hourly = (now_utc - last_time).total_seconds() >= 3600.0

        if is_error:
            print(f"❌ Kraken momentum: {report.get('details', 'unknown error')}")
        elif has_trade:
            for line in msg_lines:
                if line.startswith(("🛒", "🔄")):
                    print(line)
                    break
        elif has_fail:
            print(f"⚠️ Kraken momentum {action}: {report.get('details', '')}")
        elif action == "PENDING_AI_REVIEW":
            for line in msg_lines:
                print(line)
            should_notify = True
        elif hourly:
            pos_lines = []
            this_value_eur = 0.0
            for p in my_positions:
                sym = p["symbol"]
                ss = new_state.get(sym, {})
                ent = ss.get("entry_price", p["current_price"])
                cur_p = p["current_price"]
                pl = (cur_p - ent) / ent * 100.0 if ent else 0.0
                pos_lines.append(f"{base_symbol(sym)} {round(pl,1)}%")
                this_value_eur += p["value_eur"]
            pos_str = " | ".join(pos_lines) if pos_lines else ""
            other_value_eur = sum(p["value_eur"] for p in all_positions if p["symbol"] not in state)
            print(f"💰 Kraken momentum: {round(cash_eur,2)}€ free · this: {round(this_value_eur,2)}€ {pos_str} · other: {round(other_value_eur,2)}€")
            should_notify = True
            notify_state["last_notify_time"] = now_utc.isoformat().replace("+00:00", "Z")
        # else: silent

        db_save_notify_state(db_conn, EXCHANGE_NAME, notify_state)
        close_connection(db_conn)

    # 1. Fetch tickers + balances. Persist prices FIRST (shared price writer).
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
    all_positions = []  # every coin held in the shared wallet
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
            all_positions.append({'symbol': sym, 'coin': coin, 'qty': qty,
                                  'current_price': price, 'value_eur': value_eur})

    # Positions THIS strategy owns = balance coins recorded in our state.
    my_positions = [p for p in all_positions if p["symbol"] in state]

    report["portfolio_equity"] = portfolio_value
    report["buying_power"] = cash_eur
    report["open_positions_count"] = len(my_positions)
    report["total_open_count"] = len(all_positions)

    # Initialise local per-symbol state for our own positions.
    new_state = {}
    for pos in my_positions:
        sym = pos['symbol']
        new_state[sym] = state.get(sym, {})
        if "entry_price" not in new_state[sym] or not new_state[sym].get("entry_price"):
            ep, et = get_entry_price_and_time(sym, pos['current_price'])
            new_state[sym]["entry_price"] = ep
            new_state[sym]["entry_time"] = et
        if not new_state[sym].get("entry_time"):
            new_state[sym]["entry_time"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        new_state[sym].setdefault("peak_plpc", 0.0)
        new_state[sym]["quantity"] = pos['qty']

    # ---------------------------------------------------------------
    # 2. Manage open positions (exit logic)
    # ---------------------------------------------------------------
    managed_any = False
    # Stale rotation tracking — flag at most one stale position.
    can_rotate = False
    stale = None

    for pos in my_positions[:]:
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

        # Trailing take-profit
        if peak_plpc >= TTP_PEAK_PCT and unrealized_plpc <= (peak_plpc - TTP_GIVEBACK_PCT):
            sell, reason = True, (f"Trailing TP (peak +{round(peak_plpc,2)}% -> "
                                  f"+{round(unrealized_plpc,2)}%)")
        # Profit lock
        elif peak_plpc >= PLOCK_PEAK_PCT and unrealized_plpc < PLOCK_FLOOR_PCT:
            sell, reason = True, (f"Profit lock (peak +{round(peak_plpc,2)}% -> "
                                  f"+{round(unrealized_plpc,2)}%)")
        # Stop-loss
        elif unrealized_plpc <= STOP_LOSS_PCT:
            sell, reason = True, f"Stop-loss ({round(unrealized_plpc,2)}% <= {STOP_LOSS_PCT}%)"
        # Breakeven protection: armed a small gain, now back at the fee floor
        elif peak_plpc >= BREAKEVEN_PEAK_PCT and unrealized_plpc <= ROUND_TRIP_FEE_PCT:
            sell, reason = True, (f"Breakeven protection (peak +{round(peak_plpc,2)}% -> "
                                  f"+{round(unrealized_plpc,2)}%, fee floor +{ROUND_TRIP_FEE_PCT}%)")
        # Hard max-hold time-stop
        elif age_hours >= MAX_HOLD_HOURS:
            sell, reason = True, f"Max-hold time-stop ({round(age_hours,1)}h)"

        pos_report = {"symbol": symbol, "unrealized_plpc": round(unrealized_plpc, 2),
                      "age_hours": round(age_hours, 2),
                      "action": "HOLD", "reason": reason or f"Hold (peak +{round(peak_plpc,2)}%)"}

        # Stale-rotation flag: held >45min and flat, OR held >1.5h — only one,
        # and only if not already being sold for a hard reason.
        is_stale = ((age_hours >= STALE_FLAT_HOURS and unrealized_plpc < STALE_FLAT_PLPC)
                    or age_hours >= STALE_MAX_HOURS)
        if is_stale and not sell and not can_rotate:
            can_rotate = True
            stale = {"symbol": symbol, "qty": qty, "entry_price": entry_price,
                     "current_price": current_price, "unrealized_plpc": unrealized_plpc,
                     "age_hours": age_hours}
            pos_report["reason"] += " [FLAGGED FOR ROTATION]"

        if sell:
            try:
                exchange.load_markets()
                fqty = float(exchange.amount_to_precision(symbol, qty))
                res = exchange.create_market_sell_order(symbol, fqty)
                pos_report["action"] = "SELL"
                managed_any = True
                should_notify = True
                msg_lines.append(f"🔄 **Πωλήθηκε {symbol} (Kraken momentum)**: {reason}")
                log_trade(db_conn, action="SELL", ticker=symbol,
                          signal_strength="EXIT", momentum_pct=0.0,
                          entry_price=entry_price, current_price=current_price,
                          unrealized_plpc=unrealized_plpc / 100.0,
                          order_id=res.get("id"), quantity=qty,
                          estimated_value_eur=qty * current_price,
                          position_size_pct=0.0, portfolio_equity=portfolio_value,
                          reason=reason)
                new_state.pop(symbol, None)
                my_positions = [p for p in my_positions if p["symbol"] != symbol]
                all_positions = [p for p in all_positions if p["symbol"] != symbol]
                if stale and symbol == stale["symbol"]:
                    can_rotate = False
                    stale = None
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
    # 3. AI Gate FIRST — exits above always run; entries gated here.
    # ---------------------------------------------------------------
    gates = load_ai_gates()
    if gates.get("script_paused"):
        reason = gates.get("reason") or "no reason given"
        print(f"AI GATE: script paused — {reason}", file=sys.stderr)
        report["action_taken"] = "SKIP"
        report["details"] = f"AI gate paused: {reason}"
        finalize()
        return
    consulting = bool(gates.get("consult_on_entry"))
    if consulting:
        print(f"AI GATE: consult on entry active — throttling size & conviction "
              f"({gates.get('reason', '')})", file=sys.stderr)

    # ---------------------------------------------------------------
    # 4. Risk gates before any buy
    # ---------------------------------------------------------------
    skip, skip_reason = False, ""
    if len(all_positions) >= MAX_TOTAL_OPEN:
        skip, skip_reason = True, f"Global open-position cap reached ({MAX_TOTAL_OPEN})."
    elif len(my_positions) >= MAX_OPEN_MOMENTUM and not can_rotate:
        skip, skip_reason = True, f"Momentum position cap reached ({MAX_OPEN_MOMENTUM})."
    elif cash_eur < MIN_TRADE_EUR and not can_rotate:
        skip, skip_reason = True, "No buying power."
    elif trades_today(db_conn) >= MAX_TRADES_PER_DAY:
        skip, skip_reason = True, f"Daily trade cap reached ({MAX_TRADES_PER_DAY})."
    else:
        rpnl = realized_pnl_today_pct(db_conn)
        if rpnl <= DAILY_LOSS_BREAKER_PCT:
            skip, skip_reason = True, f"Daily loss breaker tripped ({round(rpnl,2)}% <= {DAILY_LOSS_BREAKER_PCT}%)."
            should_notify = True
            msg_lines.append(f"🚨 **Kraken momentum daily loss breaker**: {round(rpnl,2)}% "
                             f"today — entries halted until 00:00 UTC.")

    if skip:
        report["action_taken"] = "SKIP"
        report["details"] = skip_reason
        finalize()
        return

    # ---------------------------------------------------------------
    # 5. Entry scan: momentum breakout
    # ---------------------------------------------------------------
    held = {p["symbol"] for p in all_positions}  # never enter a coin held by EITHER strategy
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

        daily = get_momentum_over(db_conn, sym, DAILY_WINDOW_MIN)
        hourly = get_one_hour_momentum(db_conn, sym)

        qualifies = ((daily is not None and daily >= DAILY_ENTRY_PCT)
                     or (hourly is not None and hourly >= HOURLY_ENTRY_PCT))
        if not qualifies:
            continue

        sig, mult = momentum_signal(daily, hourly)
        if sig is None or mult <= 0.0:
            continue

        score = max(daily if daily is not None else -999.0,
                    hourly if hourly is not None else -999.0)
        candidates.append({"symbol": sym, "price": price, "daily": daily,
                           "hourly": hourly, "signal": sig, "mult": mult,
                           "score": score})

    candidates.sort(key=lambda c: c["score"], reverse=True)
    report["scanned_assets"] = [
        {k: (round(v, 3) if isinstance(v, float) else v) for k, v in c.items()}
        for c in candidates[:8]
    ]

    if not candidates:
        report["action_taken"] = "SKIP"
        report["details"] = "No momentum breakout setup passed the filters."
        finalize()
        return

    best = candidates[0]
    symbol = best["symbol"]
    current_price = best["price"]

    if consulting and best["score"] < CONSULT_MIN_SCORE:
        report["action_taken"] = "SKIP"
        report["details"] = (f"AI consult active: best score {round(best['score'],2)} "
                             f"< {CONSULT_MIN_SCORE} conviction floor.")
        finalize()
        return

    # ---------------------------------------------------------------
    # AI PER-TRADE REVIEW — every buy must be AI-approved first
    # ---------------------------------------------------------------
    pending = load_pending_review()
    now_utc = datetime.now(timezone.utc)
    execute_approved = False  # set True when AI approved a buy and we're executing

    # Process existing verdict for THIS bot
    if pending.get("status") == "approved" and pending.get("bot") == EXCHANGE_NAME:
        # AI approved our pending candidate — execute the buy on this tick
        approved_symbol = pending["symbol"]
        # Check that the symbol is still valid
        if approved_symbol in [c["symbol"] for c in candidates]:
            symbol = approved_symbol
            best = next(c for c in candidates if c["symbol"] == approved_symbol)
            current_price = best["price"]
            # Price deviation guard
            recorded = pending.get("price")
            ai_price = float(recorded) if recorded is not None else current_price
            deviation = abs(current_price - ai_price) / ai_price * 100
            if deviation > 2.0:
                clear_pending_review()
                report["details"] = (f"AI approved {approved_symbol} at €{ai_price:.2f} but "
                                     f"price moved {deviation:.1f}% (now €{current_price:.2f}) — skipping.")
                finalize()
                return
            clear_pending_review()
            execute_approved = True  # skip re-submit, proceed to buy
        else:
            # Approved coin no longer in candidates — can't buy
            report["action_taken"] = "SKIP"
            report["details"] = (f"AI approved {approved_symbol} but it no longer passes "
                                 f"entry filters — re-submitting.")
            clear_pending_review()
            # Fall through to submit new candidate below

    elif pending.get("status") == "rejected" and pending.get("bot") == EXCHANGE_NAME:
        reason = pending.get("verdict_reason", "No reason given")
        clear_pending_review()
        report["action_taken"] = "SKIP"
        report["details"] = f"AI rejected {pending['symbol']}: {reason}"
        msg_lines.append(f"❌ AI απέρριψε {pending['symbol']}: {reason}")
        should_notify = True
        finalize()
        return

    elif pending.get("status") == "pending" and pending.get("bot") != EXCHANGE_NAME:
        # Another bot has a pending review — skip this tick
        # But if it's stale (>120min), clear it so we can proceed
        stale = False
        if pending.get("created_at"):
            created = datetime.fromisoformat(pending["created_at"].replace("Z", "+00:00"))
            age = (now_utc - created).total_seconds() / 60.0
            if age > PENDING_REVIEW_TIMEOUT_MIN:
                print(f"Other bot's pending review stale ({round(age)} min) — clearing.",
                      file=sys.stderr)
                clear_pending_review()
                stale = True
        if not stale:
            report["action_taken"] = "SKIP"
            report["details"] = (f"Other bot ({pending.get('bot')}) has a pending review — "
                                 f"will re-check next tick.")
            finalize()
            return

    elif pending.get("status") == "pending" and pending.get("bot") == EXCHANGE_NAME:
        # Our own review is in progress — still waiting for AI
        # Drop stale pending (AI might have crashed / never reviewed)
        if pending.get("created_at"):
            created = datetime.fromisoformat(pending["created_at"].replace("Z", "+00:00"))
            age = (now_utc - created).total_seconds() / 60.0
            if age > PENDING_REVIEW_TIMEOUT_MIN:
                print(f"Stale pending review ({round(age)} min) — clearing.", file=sys.stderr)
                clear_pending_review()
            else:
                report["action_taken"] = "SKIP"
                report["details"] = (f"Waiting for AI review of {pending['symbol']} "
                                     f"({round(age)} min old)")
                finalize()
                return
        else:
            report["action_taken"] = "SKIP"
            report["details"] = f"Pending review active for {pending.get('symbol', '?')} — waiting"
            finalize()
            return

    # Check if we need to submit a new candidate (no pending, or pending was cleared)
    # (unless the AI already approved a buy — skip to rotation/execution)
    if not execute_approved:
        pending = load_pending_review()  # reload in case we cleared above
        if pending.get("status") is None and pending.get("bot") is None:
            # No pending review — submit the best candidate to AI
            daily_str = f"+{round(best['daily'],2)}%" if best.get('daily') is not None else "N/A"
            hourly_str = f"+{round(best['hourly'],2)}%" if best.get('hourly') is not None else "N/A"
            pending_data = {
                "bot": EXCHANGE_NAME,
                "strategy": "momentum-breakout",
                "symbol": symbol,
                "price": current_price,
                "score": round(best["score"], 4),
                "signals": {
                    "daily": round(best["daily"], 4) if best.get("daily") is not None else None,
                    "hourly": round(best["hourly"], 4) if best.get("hourly") is not None else None,
                    "signal": best.get("signal"),
                    "mult": best.get("mult"),
                },
                "momentum_desc": f"{best['signal']} (daily {daily_str}, hourly {hourly_str})",
                "created_at": now_utc.isoformat(),
                "candidate_id": str(uuid.uuid4()),
                "status": "pending",
                "verdict": None,
                "verdict_reason": None,
                "reviewed_at": None,
            }
            submitted = _submit_candidate(pending_data)
            if submitted:
                report["action_taken"] = "PENDING_AI_REVIEW"
                report["details"] = (f"Candidate {symbol} (score {round(best['score'],2)}) "
                                     f"submitted for AI review.")
                msg_lines.append(f"🤔 **{symbol} (Kraken momentum)** σε αναμονή AI αξιολόγησης "
                                 f"(score {round(best['score'],1)})")
                should_notify = True
            else:
                report["action_taken"] = "SKIP"
                report["details"] = (f"Other bot submitted first — "
                                     f"will check again next cycle.")
            finalize()
            return
    # If we reach here, the AI approved a buy and we kept the symbol — proceed below.
    # If we didn't keep the symbol, finalize() already returned.

    # If we are out of cash / at the momentum cap but flagged a stale position,
    # only rotate when the fresh signal is clearly stronger than a plain entry.
    need_rotation = (cash_eur < MIN_TRADE_EUR or len(my_positions) >= MAX_OPEN_MOMENTUM)
    if need_rotation:
        strong_enough = ((best["daily"] is not None and best["daily"] >= ROT_DAILY_PCT)
                         or (best["hourly"] is not None and best["hourly"] >= ROT_HOURLY_PCT))
        if not (can_rotate and strong_enough):
            report["action_taken"] = "SKIP"
            report["details"] = ("No free capital/slot and best new signal not strong "
                                 "enough to rotate a stale position.")
            finalize()
            return
        # Sell the stale position first to free capital/slot.
        try:
            exchange.load_markets()
            fqty = float(exchange.amount_to_precision(stale["symbol"], stale["qty"]))
            res = exchange.create_market_sell_order(stale["symbol"], fqty)
            should_notify = True
            msg_lines.append(f"🔄 **Περιστροφή (Kraken momentum)**: Πωλήθηκε στάσιμο "
                             f"**{stale['symbol']}** (+{round(stale['unrealized_plpc'],2)}% "
                             f"μετά {round(stale['age_hours'],2)}h) για {symbol}.")
            log_trade(db_conn, action="SELL", ticker=stale["symbol"],
                      signal_strength="ROTATION", momentum_pct=0.0,
                      entry_price=stale["entry_price"], current_price=stale["current_price"],
                      unrealized_plpc=stale["unrealized_plpc"] / 100.0,
                      order_id=res.get("id"), quantity=stale["qty"],
                      estimated_value_eur=stale["qty"] * stale["current_price"],
                      position_size_pct=0.0, portfolio_equity=portfolio_value,
                      reason=f"Stale rotation — freeing capital for hot {symbol}.")
            new_state.pop(stale["symbol"], None)
            my_positions = [p for p in my_positions if p["symbol"] != stale["symbol"]]
            all_positions = [p for p in all_positions if p["symbol"] != stale["symbol"]]
            save_trading_state(db_conn, EXCHANGE_NAME, new_state)
            time.sleep(1.5)
            balance = exchange.fetch_balance()
            cash_eur = balance['total'].get('EUR', 0.0)
        except Exception as e:
            report["action_taken"] = "SELL_FAILED"
            report["details"] = f"Failed to rotate stale {stale['symbol']}: {e}"
            print(f"Rotation sell failed: {e}", file=sys.stderr)
            finalize()
            return

    # Sizing: deploy fraction * conviction tier, then a volatility-adjusted risk
    # cap against the hard stop-loss distance.
    deploy_fraction = CONSULT_DEPLOY_FRACTION if consulting else DEPLOY_FRACTION
    order_size_eur = cash_eur * deploy_fraction * best["mult"]

    stop_pct = abs(STOP_LOSS_PCT)
    risk_cap_eur = (RISK_PER_TRADE_PCT / 100.0 * portfolio_value) / (stop_pct / 100.0)
    if risk_cap_eur < order_size_eur:
        order_size_eur = risk_cap_eur
    order_size_eur = min(order_size_eur, cash_eur)

    if order_size_eur < MIN_TRADE_EUR:
        report["action_taken"] = "SKIP"
        report["details"] = "Order size below Kraken minimum."
        finalize()
        return

    qty = order_size_eur / current_price
    exchange.load_markets()
    mkt = exchange.market(symbol)
    min_amt = mkt['limits']['amount']['min']
    if min_amt and qty < min_amt:
        report["action_taken"] = "SKIP"
        report["details"] = (f"Order qty ({qty:.2f} {mkt['base']}) below exchange minimum "
                             f"({min_amt} {mkt['base']}).")
        finalize()
        return
    d_val = best["daily"] if best["daily"] is not None else -999.0
    h_val = best["hourly"] if best["hourly"] is not None else -999.0
    if h_val >= d_val and best["hourly"] is not None:
        momentum_desc = f"{best['signal']} (+{round(best['hourly'],2)}% 1h)"
    else:
        momentum_desc = f"{best['signal']} (+{round(best['daily'],2)}% daily)"
    try:
        exchange.load_markets()
        fqty = float(exchange.amount_to_precision(symbol, qty))
        res = exchange.create_market_buy_order(symbol, fqty)
        fill_price, fill_qty = extract_fill(res, current_price)
        if fill_qty is None:
            fill_qty = fqty
        actual_value = fill_qty * fill_price
        report["action_taken"] = "BUY"
        report["details"] = f"Bought {symbol} for EUR {round(actual_value,2)} @ {fill_price}."
        should_notify = True
        msg_lines.append(f"🛒 **Αγοράστηκε {symbol} (Kraken momentum)** "
                         f"(EUR {round(actual_value,2)} @ {fill_price} — {momentum_desc})")
        new_state[symbol] = {
            "entry_price": fill_price,
            "entry_time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "peak_plpc": 0.0,
            "quantity": fill_qty,
        }
        save_trading_state(db_conn, EXCHANGE_NAME, new_state)
        my_positions.append({"symbol": symbol, "coin": symbol.split('/')[0],
                            "qty": fill_qty, "current_price": fill_price,
                            "value_eur": actual_value})
        log_trade(db_conn, action="BUY", ticker=symbol,
                  signal_strength=best["signal"],
                  momentum_pct=best["score"], entry_price=fill_price,
                  current_price=fill_price, unrealized_plpc=0.0,
                  order_id=res.get("id"), quantity=fill_qty,
                  estimated_value_eur=actual_value,
                  position_size_pct=actual_value / portfolio_value * 100.0,
                  portfolio_equity=portfolio_value,
                  reason=f"{momentum_desc} on {symbol}. Deployed EUR {round(actual_value,2)}.")
    except Exception as e:
        report["action_taken"] = "BUY_FAILED"
        report["details"] = f"Failed to buy {symbol}: {e}"
        print(f"Buy failed: {e}", file=sys.stderr)

    finalize()


def main():
    lock_fp = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another kraken_momentum cycle is already running — skipping this tick.",
              file=sys.stderr)
        return
    try:
        run_cycle()
    except Exception as e:
        import traceback
        print(f"🚨 **Kraken momentum CRASHED**: {e}")
        print(f"ALERT: kraken_momentum crashed: {e}\n{traceback.format_exc()}",
              file=sys.stderr)
    finally:
        try:
            fcntl.flock(lock_fp, fcntl.LOCK_UN)
            lock_fp.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
