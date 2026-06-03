"""kraken_pullback.py — Kraken crypto strategy, redesigned (pullback-in-uptrend).

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
import uuid
import fcntl
import ccxt
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

# db_prices lives in traders/extreme/ — add it to the import path so this
# script works when invoked from traders/crypto_trades/.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "extreme"))
from db_prices import (
                       DEBUG,get_connection, insert_prices, get_one_hour_momentum,
                       close_connection, base_symbol,
                       load_trading_state, save_trading_state,
                       load_notify_state, save_notify_state as db_save_notify_state,
                       log_trade as db_log_trade,
                       get_momentum_over, get_range_pct, get_recent_high, get_recent_low,
                       last_exit_time, trades_today, realized_pnl_today_pct)

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

EXCHANGE_NAME = "kraken-pullback"
# DB prices are always written under exchange='kraken' (hardcoded in db_prices.insert_prices).
PRICE_EXCHANGE = "kraken"
LOG_DIR = os.path.join(ROOT_DIR, "logs")
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
RR_MIN = 2.0               # require room-to-6h-high >= RR_MIN × stop distance (reward:risk gate)

# --- Exits ----------------------------------------------------------------
MIN_HARD_STOP_PCT = 2.0
MAX_HARD_STOP_PCT = 8.0    # cap the vol-widened stop so a crazy range can't risk the account
TRAIL_ARM_PCT = 2.5        # arm the trailing TP only after a real +2.5% peak
TRAIL_GIVEBACK_FRAC = 0.40       # let winners run: give back 40% of the peak gain...
TRAIL_GIVEBACK_MIN_PCT = 1.0     # ...but never trail tighter than this absolute floor
HARD_TP_CAP_PCT = 6.0      # absolute take-profit ceiling
MAX_HOLD_HOURS = 12.0      # only force-exit a *dead* (net-neg, trend-broken) bag
STALE_HOLD_HOURS = 18.0    # free capital from a stalled, trend-broken, barely-green bag

# --- Position sizing / risk -----------------------------------------------
DEPLOY_FRACTION = 0.60     # deploy ~60% of cash into the single best setup
# Volatility-adjusted cap: never risk more than RISK_PER_TRADE_PCT of equity if
# the stop is hit. size = riskEUR / (stop% / 100). Generous on purpose — it only
# trims size on high-volatility coins whose stop sits far away; tight setups
# still deploy the full DEPLOY_FRACTION.
RISK_PER_TRADE_PCT = 4.0
CONSULT_DEPLOY_FRACTION = 0.5  # when AI consult_on_entry is set, halve exposure
CONSULT_MIN_SCORE = 3.0        # ...and only take higher-conviction setups
MIN_TRADE_EUR = 0.45       # Kraken minimum
MAX_OPEN_SMALL = 1         # one position at a time for a small account
MAX_OPEN_LARGE = 2         # allow 2 only above EQUITY_TWO_POS
EQUITY_TWO_POS = 400.0

# --- Concurrency guard ------------------------------------------------------
# Two */5 cron jobs (or an overrun) running this script at once would double the
# orders and race on trading_state. A single flock makes overlapping runs exit.
LOCK_FILE = os.path.join(ROOT_DIR, "logs/kraken_pullback.lock")

# --- AI Gates ---------------------------------------------------------------
AI_GATE_FILE = os.path.join(ROOT_DIR, "ai_overseer/ai_gate.json")


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
PENDING_REVIEW_TIMEOUT_MIN = 120  # drop stale pending after this many minutes


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
    """Atomically write pending review data (locked against concurrent bots)."""
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
        # Re-check under exclusive lock: another bot might have just submitted
        if os.path.exists(PENDING_REVIEW_FILE):
            try:
                with open(PENDING_REVIEW_FILE) as f:
                    existing = json.load(f)
                if existing.get("status") == "pending":
                    # Another bot already submitted — don't overwrite
                    return False
            except (json.JSONDecodeError, IOError):
                pass
        write_pending_review(pending_data)
        return True
    return _with_pending_lock(_do)


COOLDOWN_MIN = 90          # per-coin cooldown after any exit (kills churn)
MAX_TRADES_PER_DAY = 4     # hard overtrading cap
DAILY_LOSS_BREAKER_PCT = -4.0  # stop trading for the UTC day past this realized loss


# ---------------------------------------------------------------------------
# Local SQL helpers (read-only; asset_prices is written by the cycle itself)
# ---------------------------------------------------------------------------
# (DB market-read helpers moved to db_prices.py — imported above)


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


def extract_fill(res, fallback_price):
    """Recover the actual average fill price and filled qty from a CCXT order.

    Kraken market orders slip, so the ticker price used to size the order is NOT
    the real entry. Prefer the order's reported average; fall back through
    price, cost/filled, and finally the pre-trade ticker price.
    """
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


# ---------------------------------------------------------------------------
# Main cycle
# ---------------------------------------------------------------------------
def run_cycle():
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "success",
        "strategy": "pullback-in-uptrend",
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

    # Print only meaningful output — silent when nothing happens.
    def finalize():
        nonlocal should_notify
        action = report.get("action_taken", "")
        is_error = report.get("status") == "error"
        has_trade = action in ("BUY", "SELL")
        has_fail = action in ("BUY_FAILED", "SELL_FAILED")

        # Hourly heartbeat timer
        last_str = notify_state.get("last_notify_time", "1970-01-01T00:00:00Z")
        last_time = datetime.fromisoformat(last_str.replace("Z", "+00:00"))
        now_utc = datetime.now(timezone.utc)
        hourly = (now_utc - last_time).total_seconds() >= 3600.0

        if is_error:
            print(f"❌ Kraken pullback: {report.get('details', 'unknown error')}")
        elif has_trade:
            # Just the one trade line
            for line in msg_lines:
                if line.startswith(("🛒", "🔄")):
                    print(line)
                    break
        elif has_fail:
            print(f"⚠️ Kraken pullback {action}: {report.get('details', '')}")
        elif action == "PENDING_AI_REVIEW":
            for line in msg_lines:
                print(line)
            should_notify = True
        elif hourly:
            pos_lines = []
            this_value_eur = 0.0
            for p in positions:
                sym = p["symbol"]
                ss = new_state.get(sym, {})
                ent = ss.get("entry_price", p["current_price"])
                cur_p = p["current_price"]
                pl = (cur_p - ent) / ent * 100.0 if ent else 0.0
                pos_lines.append(f"{base_symbol(sym)} {round(pl,1)}%")
                this_value_eur += p["value_eur"]
            pos_str = " | ".join(pos_lines) if pos_lines else ""
            other_value_eur = sum(p["value_eur"] for p in all_positions if p["symbol"] not in state)
            print(f"💰 Kraken pullback: {round(cash_eur,2)}€ free · this: {round(this_value_eur,2)}€ {pos_str} · other: {round(other_value_eur,2)}€")
            should_notify = True
            notify_state["last_notify_time"] = now_utc.isoformat().replace("+00:00", "Z")
        # else: silent — no action, no error, no hourly tick

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

    # Only manage coins THIS strategy owns (recorded in our state). On a shared
    # Kraken wallet, momentum may hold other coins — never adopt/sell those.
    all_positions = list(positions)
    positions = [p for p in positions if p["symbol"] in state]

    report["portfolio_equity"] = portfolio_value
    report["buying_power"] = cash_eur
    report["open_positions_count"] = len(positions)

    # Initialise local per-symbol state (entry price/time, peak).
    new_state = {}
    for pos in positions:
        sym = pos['symbol']
        new_state[sym] = state.get(sym, {})
        if not new_state[sym].get("entry_price"):
            ep, et = get_entry_price_and_time(sym, pos['current_price'])
            new_state[sym]["entry_price"] = ep
            new_state[sym]["entry_time"] = et
        new_state[sym].setdefault("peak_plpc", 0.0)
        # Track the real on-exchange quantity so SELLs and PnL never guess.
        new_state[sym]["quantity"] = pos['qty']

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

        # dynamic stop: at least MIN_HARD_STOP_PCT, wider for volatile coins, but
        # capped at MAX_HARD_STOP_PCT so an extreme 6h range can't risk the account.
        rng_6h = get_range_pct(db_conn, symbol, 360, price_exchange=PRICE_EXCHANGE)
        # Tighten the stop to 60% of its distance when the day is already bleeding
        # (realized <= -2.0%): protect remaining capital instead of giving losers room.
        rpnl_today = realized_pnl_today_pct(db_conn, exchange_name=EXCHANGE_NAME, round_trip_fee_pct=ROUND_TRIP_FEE_PCT)
        stop_tighten = 0.6 if rpnl_today <= -2.0 else 1.0
        effective_stop = -min(MAX_HARD_STOP_PCT,
                              max(MIN_HARD_STOP_PCT, 0.5 * (rng_6h or MIN_HARD_STOP_PCT * 2))) * stop_tighten

        if unrealized_plpc <= effective_stop:
            sell, reason = True, f"Hard stop ({round(unrealized_plpc,2)}% <= {round(effective_stop,2)}%, rng6h={round(rng_6h or 0,2)}%)"
        elif unrealized_plpc >= HARD_TP_CAP_PCT:
            sell, reason = True, f"Take-profit cap (+{round(unrealized_plpc,2)}% >= +{HARD_TP_CAP_PCT}%)"
        elif peak_plpc >= TRAIL_ARM_PCT and unrealized_plpc <= (
                peak_plpc - max(TRAIL_GIVEBACK_MIN_PCT, peak_plpc * TRAIL_GIVEBACK_FRAC)):
            giveback = max(TRAIL_GIVEBACK_MIN_PCT, peak_plpc * TRAIL_GIVEBACK_FRAC)
            sell, reason = True, (f"Trailing TP (peak +{round(peak_plpc,2)}% -> "
                                  f"+{round(unrealized_plpc,2)}%, giveback {round(giveback,2)}%, "
                                  f"net +{round(unrealized_plpc-ROUND_TRIP_FEE_PCT,2)}%)")
        elif age_hours >= MAX_HOLD_HOURS and unrealized_plpc < 0:
            # Only time-stop a DEAD bag: net-negative AND trend broken. Never a winner.
            trend_3h = get_momentum_over(db_conn, symbol, TREND_3H_MIN, price_exchange=PRICE_EXCHANGE)
            if trend_3h is not None and trend_3h < 0:
                sell, reason = True, (f"Max-hold dead-bag exit ({round(age_hours,1)}h, "
                                      f"{round(unrealized_plpc,2)}%, 3h trend {round(trend_3h,2)}%)")
        elif (age_hours >= STALE_HOLD_HOURS
              and ROUND_TRIP_FEE_PCT < unrealized_plpc < TRAIL_ARM_PCT):
            # Opportunity-cost exit: a stalled, trend-broken bag that is barely
            # green (net-positive after fees but never armed the trailing TP).
            # Free the capital for a fresh setup — but only bank a *net win*, so
            # this never becomes the flat/fee-loss churn that killed v1.
            trend_3h = get_momentum_over(db_conn, symbol, TREND_3H_MIN, price_exchange=PRICE_EXCHANGE)
            if trend_3h is not None and trend_3h < 0:
                sell, reason = True, (f"Stale-winner exit ({round(age_hours,1)}h, "
                                      f"net +{round(unrealized_plpc-ROUND_TRIP_FEE_PCT,2)}%, "
                                      f"3h trend {round(trend_3h,2)}%)")

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
                msg_lines.append(f"🔄 **Πωλήθηκε {symbol} (Kraken pullback)**: {reason}")
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
    # 3. AI Gate FIRST — if the overseer paused entries, skip before we spend
    #    any further DB queries on risk gates / the entry scan. (Exits in step 2
    #    always run regardless, so a paused script can still cut losers.)
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
    max_open = MAX_OPEN_LARGE if portfolio_value >= EQUITY_TWO_POS else MAX_OPEN_SMALL
    skip, skip_reason = False, ""
    if len(positions) >= max_open:
        skip, skip_reason = True, f"Max positions reached ({max_open})."
    elif cash_eur < MIN_TRADE_EUR:
        skip, skip_reason = True, "No buying power."
    elif trades_today(db_conn, exchange_name=EXCHANGE_NAME) >= MAX_TRADES_PER_DAY:
        skip, skip_reason = True, f"Daily trade cap reached ({MAX_TRADES_PER_DAY})."
    else:
        rpnl = realized_pnl_today_pct(db_conn, exchange_name=EXCHANGE_NAME, round_trip_fee_pct=ROUND_TRIP_FEE_PCT)
        if rpnl <= DAILY_LOSS_BREAKER_PCT:
            skip, skip_reason = True, f"Daily loss breaker tripped ({round(rpnl,2)}% <= {DAILY_LOSS_BREAKER_PCT}%)."
            should_notify = True
            msg_lines.append(f"🚨 **Kraken pullback daily loss breaker**: {round(rpnl,2)}% "
                             f"today — entries halted until 00:00 UTC.")

    if skip:
        report["action_taken"] = "SKIP"
        report["details"] = skip_reason
        finalize()
        return

    # ---------------------------------------------------------------
    # 5. Entry scan: pullback inside a confirmed higher-TF uptrend
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
        lx = last_exit_time(db_conn, sym, exchange_name=EXCHANGE_NAME)
        if lx is not None and (now - lx) < timedelta(minutes=COOLDOWN_MIN):
            continue

        # volatility floor — moves must be able to clear the fee
        rng = get_range_pct(db_conn, sym, VOL_WINDOW_MIN, price_exchange=PRICE_EXCHANGE)
        if rng is None or rng < VOL_FLOOR_PCT:
            continue

        # higher-timeframe uptrend
        t3 = get_momentum_over(db_conn, sym, TREND_3H_MIN, price_exchange=PRICE_EXCHANGE)
        t6 = get_momentum_over(db_conn, sym, TREND_6H_MIN, price_exchange=PRICE_EXCHANGE)
        if t3 is None or t6 is None or t3 < TREND_3H_MIN_PCT or t6 <= 0:
            continue

        # anti blow-off top guard
        h1 = get_one_hour_momentum(db_conn, sym)
        if h1 is not None and h1 > BLOWOFF_GUARD_1H_PCT:
            continue

        # pullback: price must be at least PULLBACK_MIN_PCT below the 1h high
        hi1h = get_recent_high(db_conn, sym, 60, price_exchange=PRICE_EXCHANGE)
        if hi1h is None or hi1h <= 0:
            continue
        pullback = (hi1h - price) / hi1h * 100.0
        if pullback < PULLBACK_MIN_PCT:
            continue

        # R:R gate: upside room to the 6h high must be >= RR_MIN × the stop
        # distance this setup would use. Rejects setups with little headroom
        # above where the stop sits (asymmetric risk).
        hi6h = get_recent_high(db_conn, sym, 360, price_exchange=PRICE_EXCHANGE)
        stop_dist = min(MAX_HARD_STOP_PCT,
                        max(MIN_HARD_STOP_PCT, 0.5 * (rng or MIN_HARD_STOP_PCT * 2)))
        if hi6h is None or hi6h <= 0:
            continue
        room_pct = (hi6h - price) / price * 100.0
        if room_pct < RR_MIN * stop_dist:
            continue

        # bounce gate: require price to be bouncing off lows,
        # not still falling (rejects falling knives)
        price_5m = get_momentum_over(db_conn, sym, 5, price_exchange=PRICE_EXCHANGE)
        low15m = get_recent_low(db_conn, sym, 15, price_exchange=PRICE_EXCHANGE)
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

    # AI consult_on_entry enforcement: only take higher-conviction setups.
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
        if approved_symbol not in [c["symbol"] for c in candidates]:
            report["details"] = (f"AI approved {approved_symbol} but it's no longer "
                                 f"a candidate — re-submitting {symbol}.")
            clear_pending_review()
        else:
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

    elif pending.get("status") == "rejected" and pending.get("bot") == EXCHANGE_NAME:
        # AI said no — clear and report
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

    # No pending review for this bot — submit the best candidate to AI
    # (unless the AI already approved a buy — skip to sizing/execution)
    if not execute_approved:
        pending = load_pending_review()  # reload in case we cleared stale above
        if pending.get("status") is not None or pending.get("bot") is not None:
            # Another bot's verdict or stale state exists — don't overwrite
            report["action_taken"] = "SKIP"
            report["details"] = (f"Pending state exists ({pending.get('status', '?')} "
                                 f"bot={pending.get('bot', '?')}) — not overwriting.")
            finalize()
            return
        pending_data = {
            "bot": EXCHANGE_NAME,
            "symbol": symbol,
            "price": current_price,
            "score": round(best["score"], 4),
            "signals": {
                "t3": round(best["t3"], 4) if best.get("t3") is not None else None,
                "t6": round(best["t6"], 4) if best.get("t6") is not None else None,
                "rng": round(best["rng"], 4) if best.get("rng") is not None else None,
                "pullback": round(best["pullback"], 4) if best.get("pullback") is not None else None,
                "h1": round(best["h1"], 4) if best.get("h1") is not None else None,
            },
            "momentum_desc": (f"3h +{round(best['t3'],2) if best.get('t3') else 0}%, "
                              f"pullback -{round(best['pullback'],2) if best.get('pullback') else 0}%"),
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
            msg_lines.append(f"🤔 **{symbol} (Kraken pullback)** σε αναμονή AI αξιολόγησης "
                             f"(score {round(best['score'],1)})")
            should_notify = True
        else:
            report["action_taken"] = "SKIP"
            report["details"] = (f"Other bot submitted first — "
                                 f"will check again next cycle.")
        finalize()
        return

    # Sizing: one concentrated, well-chosen trade. Start from ~97% of cash
    # (halved when the AI asked us to consult), then apply a volatility-adjusted
    # cap so a wide-stop (volatile) coin can't risk more than RISK_PER_TRADE_PCT
    # of equity. Tight-stop setups still deploy the full fraction.
    deploy_fraction = CONSULT_DEPLOY_FRACTION if consulting else DEPLOY_FRACTION
    order_size_eur = cash_eur * deploy_fraction

    rng_6h_best = get_range_pct(db_conn, symbol, 360, price_exchange=PRICE_EXCHANGE)
    stop_pct = min(MAX_HARD_STOP_PCT,
                   max(MIN_HARD_STOP_PCT, 0.5 * (rng_6h_best or MIN_HARD_STOP_PCT * 2)))
    risk_cap_eur = (RISK_PER_TRADE_PCT / 100.0 * portfolio_value) / (stop_pct / 100.0)
    if risk_cap_eur < order_size_eur:
        order_size_eur = risk_cap_eur
    order_size_eur = min(order_size_eur, cash_eur)

    if order_size_eur < MIN_TRADE_EUR:
        report["action_taken"] = "SKIP"
        report["details"] = "Order size below Kraken minimum."
        finalize()
        return

    exchange.load_markets()
    mkt = exchange.market(symbol)
    qty = order_size_eur / current_price
    min_amt = mkt['limits']['amount']['min']
    if min_amt and qty < min_amt:
        report["action_taken"] = "SKIP"
        report["details"] = (f"Order qty ({qty:.2f} {mkt['base']}) below exchange minimum "
                             f"({min_amt} {mkt['base']}).")
        finalize()
        return
    momentum_desc = (f"PULLBACK_IN_UPTREND (3h +{round(best['t3'],2)}%, "
                     f"dip -{round(best['pullback'],2)}%, vol {round(best['rng'],2)}%)")
    try:
        fqty = float(exchange.amount_to_precision(symbol, qty))
        res = exchange.create_market_buy_order(symbol, fqty)
        # Use the ACTUAL average fill price/qty — market orders slip, and the
        # ticker price would otherwise corrupt every downstream PnL/stop calc.
        fill_price, fill_qty = extract_fill(res, current_price)
        if fill_qty is None:
            fill_qty = fqty
        actual_value = fill_qty * fill_price
        report["action_taken"] = "BUY"
        report["details"] = f"Bought {symbol} for EUR {round(actual_value,2)} @ {fill_price}."
        should_notify = True
        msg_lines.append(f"🛒 **Αγοράστηκε {symbol} (Kraken pullback)** "
                         f"(EUR {round(actual_value,2)} @ {fill_price} — {momentum_desc})")
        new_state[symbol] = {
            "entry_price": fill_price,
            "entry_time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "peak_plpc": 0.0,
            "quantity": fill_qty,
        }
        save_trading_state(db_conn, EXCHANGE_NAME, new_state)
        positions.append({"symbol": symbol, "coin": symbol.split('/')[0],
                          "qty": fill_qty, "current_price": fill_price,
                          "value_eur": actual_value})
        log_trade(db_conn, action="BUY", ticker=symbol,
                  signal_strength="PULLBACK_IN_UPTREND",
                  momentum_pct=best["t3"], entry_price=fill_price,
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
    # Single-instance lock: a second */5 job (or a slow overrun) that overlaps
    # this run would double orders and race trading_state. Bail out if held.
    lock_fp = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another kraken_pullback cycle is already running — skipping this tick.",
              file=sys.stderr)
        return
    try:
        run_cycle()
    except Exception as e:
        import traceback
        # Loud, single-line alert so the cron/notifier wrapper surfaces a crash
        # instead of failing silently with real money on the exchange.
        print(f"🚨 **Kraken pullback CRASHED**: {e}")
        print(f"ALERT: kraken_pullback crashed: {e}\n{traceback.format_exc()}",
              file=sys.stderr)
    finally:
        try:
            fcntl.flock(lock_fp, fcntl.LOCK_UN)
            lock_fp.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
