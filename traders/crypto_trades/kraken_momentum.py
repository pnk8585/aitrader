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

_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import json
import uuid
import time
import fcntl
import ccxt
from datetime import datetime, timezone, timedelta
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
from traders.common.gates import check_gate, load_ai_gates
from traders.strategies.momentum import config as MO
from traders.common.atr_stops import compute_atr_from_prices, compute_atr_stop, fetch_atr_pct
from traders.common.pnl_notify import format_sell_pnl_auto
from traders.common.kelly import kelly_position_size
from traders.common.laddered_tp import should_take_partial_profit as check_ladder_tp
from traders.strategies.momentum.exits import is_stale_rotation_candidate, should_exit_momentum
from traders.strategies.regime import detect_regime
from traders.strategies.regime.router import should_enter
from traders.common.dca_entry import dca_entry_decision, dca_buy_qty, MAX_DCA_LEVEL

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

# Paper mode: prefix exchange name so paper trades are recorded separately
if os.environ.get("AITRADER_MODE") == "paper":
    EXCHANGE_NAME = f"paper-{EXCHANGE_NAME}"
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
ATR_PERIOD = MO.ATR_PERIOD
MAX_ATR_PCT = MO.MAX_ATR_PCT
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


# (DB market-read helpers moved to db_prices.py — imported above)


# ---------------------------------------------------------------------------
# Trade logging shim
# ---------------------------------------------------------------------------
def log_trade(db_conn, action, ticker, signal_strength, momentum_pct, entry_price,
              current_price, unrealized_plpc, order_id, quantity,
              estimated_value_eur, position_size_pct, portfolio_equity, reason,
              **kwargs):
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
        **kwargs,
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
# ATR volatility filter
# ---------------------------------------------------------------------------
_ATR_CACHE = {}  # symbol -> atr_pct (cleared each cycle)


def _clear_atr_cache():
    _ATR_CACHE.clear()


def atr_pct(symbol):
    """Wilder ATR(14) as % of current close via 1h candles. Cached per cycle."""
    cached = _ATR_CACHE.get(symbol)
    if cached is not None:
        return cached
    val = fetch_atr_pct(exchange, symbol, period=ATR_PERIOD)
    _ATR_CACHE[symbol] = val
    return val


# ---------------------------------------------------------------------------
# Main cycle
# ---------------------------------------------------------------------------
def run_cycle():
    _clear_atr_cache()

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "success",
        "strategy": "momentum-breakout",
        "positions_managed": [],
        "scanned_assets": [],
        "action_taken": "NONE",
        "details": "",
        "regime": "unknown",
    }

    db_conn = get_connection()
    try:
        db_conn.rollback()  # Clear any stale failed transaction from prior cycle
    except Exception:
        pass
    state = load_trading_state(db_conn, EXCHANGE_NAME)
    cycle_regime = detect_regime(db_conn, "BTC") or "unknown"
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
            try:
                db_conn.rollback()
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
            try:
                db_conn.rollback()
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

        atr_stop = None
        if MO.USE_ATR_STOPS:
            raw_atr = atr_pct(symbol)
            if raw_atr is not None:
                atr_stop = -(raw_atr * MO.ATR_STOP_MULTIPLIER)
        sell, reason = should_exit_momentum(
            unrealized_plpc=unrealized_plpc,
            peak_plpc=peak_plpc,
            age_hours=age_hours,
            atr_stop_pct=atr_stop,
            tp_level=ss.get("tp_level", 0),
            tp_sold_qty=ss.get("tp_sold_qty", 0.0),
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
                msg_lines.append(f"🔄 **Πωλήθηκε {symbol} (Kraken momentum)**: {reason}"
                                 f"{format_sell_pnl_auto(entry_price, current_price, fqty)}")
                log_trade(db_conn, action="SELL", ticker=symbol,
                          signal_strength="EXIT", momentum_pct=0.0,
                          entry_price=entry_price, current_price=current_price,
                          unrealized_plpc=unrealized_plpc / 100.0,
                          order_id=_order_res.get("id"), quantity=qty,
                          estimated_value_eur=qty * current_price,
                          position_size_pct=0.0, portfolio_equity=portfolio_value,
                          reason=reason, regime=cycle_regime,
                          strategy_name=EXCHANGE_NAME)
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

        # DCA follow-up for existing positions
        if MO.USE_DCA_ENTRY and symbol in new_state:
            dca_level = ss.get("dca_level", 0)
            sig_price = ss.get("signal_price")
            total_eur = ss.get("total_position_eur", 0)
            if sig_price and dca_level < MAX_DCA_LEVEL and total_eur > 0:
                deploy_pct = dca_entry_decision(sig_price, current_price, dca_level + 1)
                if deploy_pct > 0:
                    dca_qty = dca_buy_qty(total_eur, deploy_pct, current_price)
                    is_paper = os.environ.get("AITRADER_MODE") == "paper"
                    if is_paper:
                        fill_p = current_price
                        fill_q = dca_qty
                    else:
                        try:
                            exchange.load_markets()
                            fqty_dca = float(exchange.amount_to_precision(symbol, dca_qty))
                            res_dca = market_buy(exchange, symbol, fqty_dca, current_price)
                            _dca_res = res_dca or {}
                            fill_p, fill_q = extract_fill(_dca_res, current_price)
                            if fill_q is None:
                                fill_q = fqty_dca
                        except Exception as e:
                            print(f"DCA buy failed for {symbol}: {e}", file=sys.stderr)
                            fill_p, fill_q = None, None
                    if fill_p and fill_q:
                        old_qty = ss.get("quantity", 0)
                        old_entry = ss.get("entry_price", fill_p)
                        old_value = old_entry * old_qty
                        new_value = fill_p * fill_q
                        new_entry_price = (old_value + new_value) / (old_qty + fill_q)
                        ss["entry_price"] = new_entry_price
                        ss["quantity"] = old_qty + fill_q
                        ss["dca_level"] = dca_level + 1
                        new_state[symbol] = ss
                        pos_report["action"] = "DCA_BUY"
                        pos_report["reason"] = f"DCA level {dca_level} → {dca_level + 1} @ {fill_p}"
                        log_trade(db_conn, action="BUY", ticker=symbol,
                                  signal_strength="DCA", momentum_pct=0.0,
                                  entry_price=fill_p, current_price=fill_p,
                                  unrealized_plpc=0.0,
                                  order_id=_dca_res.get("id") if not is_paper else "paper-dca",
                                  quantity=fill_q,
                                  estimated_value_eur=fill_q * fill_p,
                                  position_size_pct=0.0, portfolio_equity=portfolio_value,
                                  reason=f"DCA level {dca_level} for {symbol} @ {fill_p}",
                                  regime=cycle_regime, strategy_name=EXCHANGE_NAME)

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
    paused, gate_msg = check_gate(db_conn)
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
    _aggressive_mode = False
    _risk_mult = 1.0
    _candidate_limit = 8  # default report limit

    # If AI Overseer is not live (paper/paused), skip AI per-trade review.
    try:
        import aitrader_registry as _orch_reg
        _ov_mode = _orch_reg.get_mode("ai-overseer")
        if _ov_mode != "live":
            _aggressive_mode = True
    except Exception:
        pass

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

        allowed, reason = should_enter(db_conn, base_symbol(sym), "momentum")
        if not allowed:
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

        # ATR volatility filter: skip during high uncertainty
        atr_p = atr_pct(sym)
        if atr_p is not None and atr_p > MAX_ATR_PCT:
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

    # Regime detection — compute and log, no entry gating yet (USE_REGIME_ROUTING=False)
    regime = detect_regime(db_conn, base_symbol(symbol))
    report["regime"] = regime

    if consulting and best["score"] < CONSULT_MIN_SCORE:
        report["action_taken"] = "SKIP"
        report["details"] = (f"AI consult active: best score {round(best['score'],2)} "
                             f"< {CONSULT_MIN_SCORE} conviction floor.")
        finalize()
        return

    # ---------------------------------------------------------------
    # AI PER-TRADE REVIEW — synchronous LLM call, no Overseer needed
    # (skipped in aggressive mode — buy directly)
    # ---------------------------------------------------------------
    if _aggressive_mode:
        execute_approved = True
    else:
        from traders.common.llm_review import review_trade

        _sig = {
            "daily": round(best.get("daily", 0) or 0, 3),
            "hourly": round(best.get("hourly", 0) or 0, 3),
            "signal": best.get("signal", ""),
            "mult": best.get("mult", 1.0),
            "score": round(best.get("score", 0) or 0, 3),
        }
        try:
            result = review_trade(
                symbol=symbol,
                strategy="momentum",
                signals=_sig,
                price=current_price,
                score=best.get("score", 0) or 0,
                portfolio_euro=portfolio_value,
                available_euro=cash_eur,
                open_positions=len(all_positions),
                db_conn=db_conn,
            )
        except Exception as e:
            print(f"LLM review failed: {e} — buying directly", file=sys.stderr)
            result = {"verdict": "REJECT", "reason": f"LLM unavailable: {e}", "confidence": 0}

        if result["verdict"] == "APPROVE":
            execute_approved = True
            should_notify = True
            msg_lines.append(
                f"✅ LLM ενέκρινε {symbol} (conf {result['confidence']}/10): {result['reason']}"
            )
        else:
            report["action_taken"] = "SKIP"
            report["details"] = f"LLM rejected {symbol}: {result['reason']}"
            msg_lines.append(f"❌ LLM απέρριψε {symbol}: {result['reason']}")
            should_notify = True
            finalize()
            return
        # Safety net: only skip when nothing was approved. Without this guard an
        # APPROVE verdict (execute_approved=True) fell through to an unconditional
        # SKIP, killing every non-aggressive momentum trade. Mirrors pullback flow.
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
            res = market_sell(exchange, stale["symbol"], fqty, stale["current_price"])
            _rot_res = res or {}
            should_notify = True
            msg_lines.append(f"🔄 **Περιστροφή (Kraken momentum)**: Πωλήθηκε στάσιμο "
                             f"**{stale['symbol']}** (+{round(stale['unrealized_plpc'],2)}% "
                             f"μετά {round(stale['age_hours'],2)}h) για {symbol}."
                             f"{format_sell_pnl_auto(stale['entry_price'], stale['current_price'], stale['qty'])}")
            log_trade(db_conn, action="SELL", ticker=stale["symbol"],
                      signal_strength="ROTATION", momentum_pct=0.0,
                      entry_price=stale["entry_price"], current_price=stale["current_price"],
                      unrealized_plpc=stale["unrealized_plpc"] / 100.0,
                      order_id=_rot_res.get("id"), quantity=stale["qty"],
                      estimated_value_eur=stale["qty"] * stale["current_price"],
                      position_size_pct=0.0, portfolio_equity=portfolio_value,
                      reason=f"Stale rotation — freeing capital for hot {symbol}.",
                      regime=cycle_regime, strategy_name=EXCHANGE_NAME)
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

    # Kelly sizing (if enabled)
    if MO.USE_KELLY_SIZING:
        try:
            stop_price = current_price * (1 + MO.STOP_LOSS_PCT / 100)  # STOP_LOSS_PCT is negative
            kelly_qty = kelly_position_size(db_conn, EXCHANGE_NAME, current_price, stop_price, cash_eur)
            if kelly_qty > 0:
                order_size_eur = kelly_qty * current_price
        except Exception:
            pass  # fallback to existing sizing

    if order_size_eur < MIN_TRADE_EUR:
        report["action_taken"] = "SKIP"
        report["details"] = "Order size below Kraken minimum."
        finalize()
        return

    qty = dca_buy_qty(order_size_eur, 0.50, current_price)
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
            "signal_price": fill_price,
            "dca_level": 0,
            "total_position_eur": order_size_eur,
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
                  reason=f"{momentum_desc} on {symbol}. Deployed EUR {round(actual_value,2)}.",
                  regime=cycle_regime, strategy_name=EXCHANGE_NAME)
    except Exception as e:
        report["action_taken"] = "BUY_FAILED"
        report["details"] = f"Failed to buy {symbol}: {e}"
        print(f"Buy failed: {e}", file=sys.stderr)

    finalize()


def main():
    # ── Orchestrator integration ──────────────────────────────────────
    # Run tracking handled by container cron_orchestrator (DB-driven)
    # ──────────────────────────────────────────────────────────────────

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

    # ── Orchestrator: schedule next run ──────────────────────────────
    # (no-op: next_run_at managed by container cron_orchestrator)


if __name__ == "__main__":
    main()
