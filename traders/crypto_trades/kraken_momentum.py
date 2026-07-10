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

from traders.common import bootstrap  # noqa: F401
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "extreme"))
from db_prices import (
                       DEBUG, get_connection, insert_prices, get_one_hour_momentum,
                       close_connection, base_symbol,
                       load_trading_state, save_trading_state,
                       load_notify_state, save_notify_state as db_save_notify_state,
                       log_trade as db_log_trade,
                       get_momentum_over, get_range_pct, last_exit_time,
                       trades_today, realized_pnl_today_pct,
                       coins_held_by_other_bots)
from traders.common.config import ROOT_DIR, ensure_log_dir
from traders.common.exchange import extract_fill, market_buy, market_sell, spread_ok
from traders.common.gates import check_gate, load_ai_gates, signal_architect_rethink
from traders.common.pending_review import (
    load_pending_review, submit_candidate as _submit_candidate, write_pending_review,
)
from traders.common.strategy import load_daily_strategy as _load_daily_strategy
from traders.strategies.momentum import config as MO
from traders.strategies.momentum.exits import is_stale_rotation_candidate, should_exit_momentum

# ---------------------------------------------------------------------------
# Config (from strategies.momentum.config)
# ---------------------------------------------------------------------------
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

EXCHANGE_NAME = MO.EXCHANGE_NAME
PRICE_EXCHANGE = MO.PRICE_EXCHANGE
CRYPTO_PAIRS = MO.CRYPTO_PAIRS
ROUND_TRIP_FEE_PCT = MO.ROUND_TRIP_FEE_PCT
DAILY_ENTRY_PCT = MO.DAILY_ENTRY_PCT
HOURLY_ENTRY_PCT = MO.HOURLY_ENTRY_PCT
DAILY_WINDOW_MIN = MO.DAILY_WINDOW_MIN
ROT_DAILY_PCT = MO.ROT_DAILY_PCT
ROT_HOURLY_PCT = MO.ROT_HOURLY_PCT
TTP_PEAK_PCT = MO.TTP_PEAK_PCT
TTP_GIVEBACK_PCT = MO.TTP_GIVEBACK_PCT
PLOCK_PEAK_PCT = MO.PLOCK_PEAK_PCT
PLOCK_FLOOR_PCT = MO.PLOCK_FLOOR_PCT
STOP_LOSS_PCT = MO.STOP_LOSS_PCT
BREAKEVEN_PEAK_PCT = MO.BREAKEVEN_PEAK_PCT
STALE_FLAT_HOURS = MO.STALE_FLAT_HOURS
STALE_FLAT_PLPC = MO.STALE_FLAT_PLPC
STALE_MAX_HOURS = MO.STALE_MAX_HOURS
MAX_HOLD_HOURS = MO.MAX_HOLD_HOURS
DEPLOY_FRACTION = MO.DEPLOY_FRACTION
RISK_PER_TRADE_PCT = MO.RISK_PER_TRADE_PCT
MIN_TRADE_EUR = MO.MIN_TRADE_EUR
MAX_OPEN_MOMENTUM = MO.MAX_OPEN_MOMENTUM
MAX_TOTAL_OPEN = MO.MAX_TOTAL_OPEN
LOCK_FILE = MO.LOCK_FILE
COOLDOWN_MIN = MO.COOLDOWN_MIN
MAX_TRADES_PER_DAY = MO.MAX_TRADES_PER_DAY
DAILY_LOSS_BREAKER_PCT = MO.DAILY_LOSS_BREAKER_PCT
CONSULT_DEPLOY_FRACTION = MO.CONSULT_DEPLOY_FRACTION
CONSULT_MIN_SCORE = MO.CONSULT_MIN_SCORE
PENDING_REVIEW_TIMEOUT_MIN = 120


def load_daily_strategy():
    return _load_daily_strategy(
        pool_bases=MO.POOL_BASES,
        adjustment_key="momentum_adjustment",
        valid_adjustments=("normal", "cautious", "skip", "aggressive"),
        default_adjustment="aggressive",
    )
# (DB market-read helpers moved to db_prices.py — imported above)


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


def sellable_qty(symbol, recorded_qty):
    """Get the actual sellable quantity for a symbol, clamped to free balance.

    Re-fetches the live balance right before selling so we never use a stale
    'total' snapshot that includes funds locked by the other Kraken strategy
    (shared-wallet pattern).  Returns the quantity to pass to
    create_market_sell_order, or 0.0 if nothing is available to sell.
    """
    try:
        bal = exchange.fetch_balance()
        coin = symbol.split('/')[0]
        free = float((bal.get('free') or {}).get(coin, 0.0))
        if free <= 0.0:
            return 0.0
        exchange.load_markets()
        safe = min(recorded_qty, free)
        precised = exchange.amount_to_precision(symbol, safe)
        return float(precised)
    except Exception:
        # If the live check itself fails, fall back to the recorded qty
        # (same behaviour as before, not worse than crashing).
        exchange.load_markets()
        return float(exchange.amount_to_precision(symbol, recorded_qty))


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
    # H3: Load strategy once per cycle and pass through
    daily_strategy = load_daily_strategy()
    
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "success",
        "strategy": "momentum-breakout",
        "strategy_missing": daily_strategy is None,
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

        if is_error:
            print(f"❌ Kraken momentum: {report.get('details', 'unknown error')}")
        elif report.get("strategy_missing"):
            # Throttled: only print once per hour to avoid 5-min spam
            _throttle_file = os.path.join(ROOT_DIR, "logs/kraken_momentum_strategy_warn.txt")
            _now = datetime.now(timezone.utc).timestamp()
            _should_warn = True
            try:
                with open(_throttle_file) as _tf:
                    _last_warn = float(_tf.read().strip())
                    if _now - _last_warn < 3600:
                        _should_warn = False
            except (FileNotFoundError, ValueError):
                pass
            if _should_warn:
                print(f"⚠️ **Kraken momentum**: Daily strategy missing or stale — running fallback config.")
                try:
                    with open(_throttle_file, "w") as _tf:
                        _tf.write(str(_now))
                except Exception:
                    pass
                should_notify = True
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
        # else: silent — heartbeat moved to hourly combined report

        db_save_notify_state(db_conn, EXCHANGE_NAME, notify_state)
        close_connection(db_conn)

    # 1. Fetch tickers + balances. Persist prices FIRST (shared price writer).
    try:
        raw_tickers = exchange.fetch_tickers(CRYPTO_PAIRS)
        tickers = raw_tickers or {}
        price_map = {
            base_symbol(sym): tickers[sym]['last']
            for sym in CRYPTO_PAIRS
            if tickers.get(sym) and tickers[sym].get('last') is not None
        }
        insert_prices(db_conn, price_map)
        raw_balance = exchange.fetch_balance()
        balance = raw_balance or {}
        # Guard: balance['total'] can be None in CCXT edge cases
        balance_total = balance.get('total') or {}

        # --- Open order reconciliation ---
        try:
            raw_orders = exchange.fetch_open_orders()
            open_orders = raw_orders or []
            now_ts = datetime.now(timezone.utc).timestamp()
            for ord in open_orders:
                if ord.get('status') == 'open':
                    sym = ord.get('symbol', '')
                    side = ord.get('side', '')
                    ord_ts = ord.get('timestamp')
                    if ord_ts and (now_ts - ord_ts / 1000) > 3600 and side == 'buy':
                        exchange.cancel_order(ord['id'], sym)
                        print(f"Reconciled stale order: {side} {sym}", file=sys.stderr)
        except Exception:
            pass

        # --- Balance reconciliation ---
        try:
            others = coins_held_by_other_bots(db_conn, EXCHANGE_NAME)
            for sym in CRYPTO_PAIRS:
                coin = sym.split('/')[0]
                qty = balance_total.get(coin, 0.0)
                if qty > 0 and coin not in [base_symbol(k) for k in state.keys()] and coin.upper() not in others:
                    ticker = tickers.get(sym)
                    if ticker and ticker.get('last'):
                        state[sym] = {'peak_plpc': 0.0, 'quantity': float(qty)}
                        print(f"Recovered orphan: {sym} ({qty})", file=sys.stderr)
            if state:
                save_trading_state(db_conn, EXCHANGE_NAME, state)
        except Exception:
            pass
    except Exception as e:
        report["status"] = "error"
        report["details"] = f"Failed to fetch market/account data: {e}"
        print(json.dumps(report), file=sys.stderr)
        close_connection(db_conn)
        return

    cash_eur = balance_total.get('EUR', 0.0)
    portfolio_value = cash_eur
    all_positions = []  # every coin held in the shared wallet
    for sym in CRYPTO_PAIRS:
        coin = sym.split('/')[0]
        qty = balance_total.get(coin, 0.0)
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

        sell, reason = should_exit_momentum(
            unrealized_plpc=unrealized_plpc,
            peak_plpc=peak_plpc,
            age_hours=age_hours,
        )

        pos_report = {"symbol": symbol, "unrealized_plpc": round(unrealized_plpc, 2),
                      "age_hours": round(age_hours, 2),
                      "action": "HOLD", "reason": reason or f"Hold (peak +{round(peak_plpc,2)}%)"}

        # Stale-rotation flag: held >45min and flat, OR held >1.5h — only one,
        # and only if not already being sold for a hard reason.
        is_stale = is_stale_rotation_candidate(unrealized_plpc, age_hours)
        if is_stale and not sell and not can_rotate:
            can_rotate = True
            stale = {"symbol": symbol, "qty": qty, "entry_price": entry_price,
                     "current_price": current_price, "unrealized_plpc": unrealized_plpc,
                     "age_hours": age_hours}
            pos_report["reason"] += " [FLAGGED FOR ROTATION]"

        if sell:
            try:
                fqty = sellable_qty(symbol, qty)
                if fqty <= 0.0:
                    pos_report["action"] = "SKIP_ZERO_BALANCE"
                    pos_report["reason"] = (f"No free {symbol.split('/')[0]} balance to sell "
                                            f"— already gone or locked by other strategy.")
                    report["positions_managed"].append(pos_report)
                    new_state.pop(symbol, None)
                    continue
                res = market_sell(exchange, symbol, fqty, current_price)
                _order_res = res or {}
                pos_report["action"] = "SELL"
                managed_any = True
                should_notify = True
                msg_lines.append(f"🔄 **Πωλήθηκε {symbol} (Kraken momentum)**: {reason}")
                log_trade(db_conn, action="SELL", ticker=symbol,
                          signal_strength="EXIT", momentum_pct=0.0,
                          entry_price=entry_price, current_price=current_price,
                          unrealized_plpc=unrealized_plpc / 100.0,
                          order_id=_order_res.get("id"), quantity=qty,
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
            _bal_total = (balance or {}).get('total') or {}
            cash_eur = _bal_total.get('EUR', 0.0)
        except Exception:
            pass

    # ---------------------------------------------------------------
    # 3. AI Gate FIRST — with condition-based auto-resume.
    #    Exits above always run regardless; only entries are gated.
    # ---------------------------------------------------------------
    paused, gate_msg = check_gate(db_conn, daily_strategy)
    if paused:
        print(f"AI GATE: script paused — {gate_msg}", file=sys.stderr)
        report["action_taken"] = "SKIP"
        report["details"] = f"AI gate paused: {gate_msg}"
        finalize()
        return
    elif gate_msg:
        # Gate auto-resumed this cycle — log and notify
        print(f"AI GATE auto-resumed: {gate_msg}", file=sys.stderr)
        msg_lines.append(f"✅ **AI Gate**: {gate_msg}")
        should_notify = True
    gates = load_ai_gates()
    consulting = bool(gates.get("consult_on_entry"))
    if consulting:
        print(f"AI GATE: consult on entry active — throttling size & conviction "
              f"({gates.get('reason', '')})", file=sys.stderr)

    # ---------------------------------------------------------------
    # 4. Risk gates before any buy
    # ---------------------------------------------------------------
    # First, process daily strategy adjustments so cautious/aggressive can
    # influence the subsequent position/trade cap checks.
    _aggressive_mode = False
    _cautious_mode = False
    _risk_mult = 1.0
    _candidate_limit = 8  # default report limit

    if daily_strategy:
        adj = daily_strategy.get("momentum_adjustment", "aggressive")
        btc_regime = daily_strategy.get("btc_regime", "neutral")
        if adj == "normal" and btc_regime in ("below", "bearish"):
            adj = "cautious"
            print("INFO: btc_regime=below/bearish overrode momentum_adjustment to cautious", file=sys.stderr)
        elif adj == "normal" and btc_regime in ("above", "bullish"):
            adj = "aggressive"
            print("INFO: btc_regime=above/bullish overrode momentum_adjustment to aggressive", file=sys.stderr)
        if adj == "skip":
            skip, skip_reason = True, "Market Architect: skip momentum entries today"
            should_notify = True
            msg_lines.append("📋 **Market Architect**: momentum entries skipped today")
        elif adj == "cautious":
            should_notify = True
            msg_lines.append("📋 **Market Architect**: momentum entries — CAUTIOUS mode")
            _cautious_mode = True
            _risk_mult = 0.5
            _candidate_limit = 3
        elif adj == "aggressive":
            _aggressive_mode = True
            msg_lines.append("📋 **Market Architect**: momentum entries — AGGRESSIVE mode")
            should_notify = True

    # Now evaluate position/trade caps with mode adjustments applied
    effective_max_momentum = MAX_OPEN_MOMENTUM
    if _aggressive_mode:
        effective_max_momentum += 1
    skip, skip_reason = False, ""
    if not skip and len(all_positions) >= MAX_TOTAL_OPEN:
        skip, skip_reason = True, f"Global open-position cap reached ({MAX_TOTAL_OPEN})."
    elif not skip and len(my_positions) >= effective_max_momentum and not can_rotate:
        skip, skip_reason = True, f"Momentum position cap reached ({effective_max_momentum})."
    elif not skip and cash_eur < MIN_TRADE_EUR and not can_rotate:
        skip, skip_reason = True, "No buying power."
    elif not skip and trades_today(db_conn, exchange_name=EXCHANGE_NAME) >= MAX_TRADES_PER_DAY:
        skip, skip_reason = True, f"Daily trade cap reached ({MAX_TRADES_PER_DAY})."
    else:
        # Shared daily buy limit across all kraken strategies (with advisory lock)
        # Only applies when Market Architect provides a daily strategy file
        if daily_strategy and not skip:
            with db_conn.cursor() as _cur:
                # PostgreSQL advisory lock serializes the check-act across both processes
                lock_key = 840271  # fixed shared key for max_daily_buys serialization
                # NOTE: pg_advisory_xact_lock is txn-scoped. No COMMIT must occur between
                # this lock acquisition and the BUY log insert, or mutual exclusion breaks.
                _cur.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
                _cur.execute(
                    "SELECT COUNT(*) FROM trade_log "
                    "WHERE exchange LIKE 'kraken%%' AND action='BUY' "
                    "AND DATE(timestamp AT TIME ZONE 'UTC') = "
                    "DATE(CURRENT_TIMESTAMP AT TIME ZONE 'UTC')")
                shared_count = _cur.fetchone()
            max_buys = int(daily_strategy.get("max_daily_buys", 3))
            if shared_count and int(shared_count[0]) >= max_buys:
                skip, skip_reason = True, f"Market Architect: max_daily_buys ({max_buys}) reached"
        if not skip:
            rpnl = realized_pnl_today_pct(db_conn, exchange_name=EXCHANGE_NAME, round_trip_fee_pct=ROUND_TRIP_FEE_PCT)
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
    # Daily strategy pair filter (Market Architect)
    _focus_syms = set(daily_strategy.get("focus_pairs", [])) if daily_strategy else set()
    _avoid_syms = set(daily_strategy.get("avoid_pairs", [])) if daily_strategy else set()
    for sym in CRYPTO_PAIRS:
        if sym in held:
            continue
        _base = base_symbol(sym)
        if _avoid_syms and _base in _avoid_syms:
            continue
        if _focus_syms and _base not in _focus_syms:
            continue
        ticker = tickers.get(sym)
        if not ticker or ticker.get('last') is None:
            continue
        price = ticker['last']

        # cooldown
        lx = last_exit_time(db_conn, sym, exchange_name=EXCHANGE_NAME)
        if lx is not None and (now - lx) < timedelta(minutes=COOLDOWN_MIN):
            continue

        daily = get_momentum_over(db_conn, sym, DAILY_WINDOW_MIN, price_exchange=PRICE_EXCHANGE)
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
    # Trim candidate list before selection (not just for reporting)
    candidates = candidates[:_candidate_limit] if _candidate_limit < len(candidates) else candidates
    report["scanned_assets"] = [
        {k: (round(v, 3) if isinstance(v, float) else v) for k, v in c.items()}
        for c in candidates[:_candidate_limit]
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
    # (skipped in aggressive mode — buy directly)
    # ---------------------------------------------------------------
    now_utc = datetime.now(timezone.utc)
    if _aggressive_mode:
        execute_approved = True
        pending = {}
    else:
        execute_approved = False  # set True when AI approved a buy and we're executing
        pending = load_pending_review()

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
            ai_price = float(recorded) if recorded is not None and float(recorded) > 0 else current_price
            deviation = abs(current_price - ai_price) / ai_price * 100 if ai_price > 0 else 0.0
            if deviation > 2.0:
                clear_pending_review()
                report["details"] = (f"AI approved {approved_symbol} at €{ai_price:.2f} but "
                                     f"price moved {deviation:.1f}% (now €{current_price:.2f}) — skipping.")
                finalize()
                return
            clear_pending_review()
            execute_approved = True  # skip re-submit, proceed to buy
        else:
            # Approved coin no longer in candidates — re-submit same cycle, like pullback does
            report["details"] = (f"AI approved {approved_symbol} but it's no longer "
                                 f"a candidate — re-submitting {symbol}.")
            clear_pending_review()
            execute_approved = False

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
    # Safety guard: only proceed to buy if we have an approved execution flag
    if not execute_approved:
        report["action_taken"] = "SKIP"
        report["details"] = "No approved trade to execute (safety guard)."
        finalize()
        return

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
            fqty = sellable_qty(stale["symbol"], stale["qty"])
            if fqty <= 0.0:
                report["action_taken"] = "ROTATION_CANCELLED"
                report["details"] = (f"Cannot rotate {stale['symbol']} — "
                                     "no free balance (already gone/locked).")
                print(f"Rotation cancelled: {report['details']}", file=sys.stderr)
                finalize()
                return
            res = exchange.create_market_sell_order(stale["symbol"], fqty)
            _rot_res = res or {}
            should_notify = True
            msg_lines.append(f"🔄 **Περιστροφή (Kraken momentum)**: Πωλήθηκε στάσιμο "
                             f"**{stale['symbol']}** (+{round(stale['unrealized_plpc'],2)}% "
                             f"μετά {round(stale['age_hours'],2)}h) για {symbol}.")
            log_trade(db_conn, action="SELL", ticker=stale["symbol"],
                      signal_strength="ROTATION", momentum_pct=0.0,
                      entry_price=stale["entry_price"], current_price=stale["current_price"],
                      unrealized_plpc=stale["unrealized_plpc"] / 100.0,
                      order_id=_rot_res.get("id"), quantity=stale["qty"],
                      estimated_value_eur=stale["qty"] * stale["current_price"],
                      position_size_pct=0.0, portfolio_equity=portfolio_value,
                      reason=f"Stale rotation — freeing capital for hot {symbol}.")
            new_state.pop(stale["symbol"], None)
            my_positions = [p for p in my_positions if p["symbol"] != stale["symbol"]]
            all_positions = [p for p in all_positions if p["symbol"] != stale["symbol"]]
            save_trading_state(db_conn, EXCHANGE_NAME, new_state)
            time.sleep(1.5)
            balance = exchange.fetch_balance()
            _bal_total = (balance or {}).get('total') or {}
            cash_eur = _bal_total.get('EUR', 0.0)
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
    risk_cap_eur = ((RISK_PER_TRADE_PCT * _risk_mult) / 100.0 * portfolio_value) / (stop_pct / 100.0)
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
    ok_spread, sp = spread_ok(exchange, symbol, MO.MAX_SPREAD_PCT)
    if not ok_spread:
        report["action_taken"] = "SKIP"
        report["details"] = f"Spread too wide ({round(sp, 3)}% > {MO.MAX_SPREAD_PCT}%)."
        finalize()
        return

    try:
        exchange.load_markets()
        fqty = float(exchange.amount_to_precision(symbol, qty))
        res = market_buy(exchange, symbol, fqty, current_price)
        _buy_res = res or {}
        fill_price, fill_qty = extract_fill(_buy_res, current_price)
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
                  order_id=_buy_res.get("id"), quantity=fill_qty,
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
