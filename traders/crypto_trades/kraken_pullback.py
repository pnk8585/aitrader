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

# Cron runs with arbitrary cwd — repo root must be on sys.path before traders imports.
_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import json
import uuid
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
                       get_momentum_over, get_range_pct, get_recent_high, get_recent_low,
                       last_exit_time, trades_today, realized_pnl_today_pct,
                       coins_held_by_other_bots)
from traders.common.config import DRY_RUN, ROOT_DIR, ensure_log_dir
from traders.common.exchange import extract_fill, market_buy, market_sell, spread_ok
from traders.common.gates import check_gate, load_ai_gates
from traders.common.kelly import kelly_position_size
from traders.strategies.pullback import config as PB
from traders.strategies.pullback.exits import compute_effective_stop, should_exit_pullback
from traders.strategies.pullback.signals import scan_pullback_candidates
from traders.strategies.regime import detect_regime

# ---------------------------------------------------------------------------
# Config (from strategies.pullback.config)
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

EXCHANGE_NAME = PB.EXCHANGE_NAME

# Paper mode: prefix exchange name so paper trades are recorded separately
if os.environ.get("AITRADER_MODE") == "paper":
    EXCHANGE_NAME = f"paper-{EXCHANGE_NAME}"
PRICE_EXCHANGE = PB.PRICE_EXCHANGE
CRYPTO_PAIRS = PB.CRYPTO_PAIRS
ROUND_TRIP_FEE_PCT = PB.ROUND_TRIP_FEE_PCT
VOL_FLOOR_PCT = PB.VOL_FLOOR_PCT
VOL_WINDOW_MIN = PB.VOL_WINDOW_MIN
TREND_3H_MIN = PB.TREND_3H_MIN
TREND_3H_MIN_PCT = PB.TREND_3H_MIN_PCT
PULLBACK_MIN_PCT = PB.PULLBACK_MIN_PCT
BLOWOFF_GUARD_1H_PCT = PB.BLOWOFF_GUARD_1H_PCT
RR_MIN = PB.RR_MIN
MIN_HARD_STOP_PCT = PB.MIN_HARD_STOP_PCT
MAX_HARD_STOP_PCT = PB.MAX_HARD_STOP_PCT
TRAIL_ARM_PCT = PB.TRAIL_ARM_PCT
TRAIL_GIVEBACK_FRAC = PB.TRAIL_GIVEBACK_FRAC
TRAIL_GIVEBACK_MIN_PCT = PB.TRAIL_GIVEBACK_MIN_PCT
HARD_TP_CAP_PCT = PB.HARD_TP_CAP_PCT
STALE_HOLD_HOURS = PB.STALE_HOLD_HOURS
DEPLOY_FRACTION = 0.12
RISK_PER_TRADE_PCT = PB.RISK_PER_TRADE_PCT
CONSULT_DEPLOY_FRACTION = PB.CONSULT_DEPLOY_FRACTION
CONSULT_MIN_SCORE = PB.CONSULT_MIN_SCORE
USE_KELLY_SIZING = PB.USE_KELLY_SIZING
MIN_TRADE_EUR = PB.MIN_TRADE_EUR
MAX_OPEN_SMALL = PB.MAX_OPEN_SMALL
MAX_OPEN_LARGE = PB.MAX_OPEN_LARGE
EQUITY_TWO_POS = PB.EQUITY_TWO_POS
LOCK_FILE = PB.LOCK_FILE
COOLDOWN_MIN = PB.COOLDOWN_MIN
MAX_TRADES_PER_DAY = PB.MAX_TRADES_PER_DAY
DAILY_LOSS_BREAKER_PCT = PB.DAILY_LOSS_BREAKER_PCT


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
        "regime": "unknown",
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
        # else: silent — heartbeat moved to hourly combined report

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

        # --- Open order reconciliation ---
        # Cancel stale open orders (older than 1h) that might be tying up EUR
        # without a corresponding entry in trading_state.
        try:
            open_orders = exchange.fetch_open_orders()
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
            pass  # non-fatal

        # --- Balance reconciliation ---
        # If we hold coins on the exchange that are NOT in our trading_state,
        # add them so exit logic can manage them (orphaned from crash/restart).
        try:
            others = coins_held_by_other_bots(db_conn, EXCHANGE_NAME)
            for sym in CRYPTO_PAIRS:
                coin = sym.split('/')[0]
                qty = balance['total'].get(coin, 0.0)
                if qty > 0 and coin not in [base_symbol(s) for s in state.keys()] and coin.upper() not in others:
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

        rng_6h = get_range_pct(db_conn, symbol, 360, price_exchange=PRICE_EXCHANGE)
        rpnl_today = realized_pnl_today_pct(
            db_conn, exchange_name=EXCHANGE_NAME, round_trip_fee_pct=ROUND_TRIP_FEE_PCT)
        effective_stop = compute_effective_stop(rng_6h, rpnl_today)
        trend_3h = get_momentum_over(db_conn, symbol, TREND_3H_MIN, price_exchange=PRICE_EXCHANGE)
        sell, reason = should_exit_pullback(
            unrealized_plpc=unrealized_plpc,
            peak_plpc=peak_plpc,
            age_hours=age_hours,
            effective_stop=effective_stop,
            trend_3h=trend_3h,
        )
        if sell and "Hard stop" in reason and rng_6h is not None:
            reason = f"{reason}, rng6h={round(rng_6h, 2)}%"

        pos_report = {"symbol": symbol, "unrealized_plpc": round(unrealized_plpc, 2),
                      "age_hours": round(age_hours, 2),
                      "action": "HOLD", "reason": reason or f"Hold (peak +{round(peak_plpc,2)}%)"}

        if sell:
            try:
                exchange.load_markets()
                fqty = float(exchange.amount_to_precision(symbol, qty))
                res = market_sell(exchange, symbol, fqty, current_price)
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
    paused, gate_msg = check_gate(db_conn)
    if paused:
        print(f"AI GATE: script paused — {gate_msg}", file=sys.stderr)
        report["action_taken"] = "SKIP"
        report["details"] = f"AI gate paused: {gate_msg}"
        finalize()
        return
    elif gate_msg:
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
    _stop_cap_mult = 1.0
    _candidate_limit = 8  # default report limit

    # If AI Overseer is not live (paper/paused), skip AI per-trade review.
    # Pullback buys directly instead of waiting up to 60 min for approval.
    try:
        import aitrader_registry as _orch_reg
        _ov_mode = _orch_reg.get_mode("ai-overseer")
        if _ov_mode != "live":
            _aggressive_mode = True
    except Exception:
        pass  # registry not available, use strategy mode as-is

    # Now evaluate position/trade caps with mode adjustments applied
    max_open = MAX_OPEN_LARGE if portfolio_value >= EQUITY_TWO_POS else MAX_OPEN_SMALL
    if _aggressive_mode:
        max_open += 1
    skip, skip_reason = False, ""
    if not skip and len(positions) >= max_open:
        skip, skip_reason = True, f"Max positions reached ({max_open})."
    elif not skip and cash_eur < MIN_TRADE_EUR:
        skip, skip_reason = True, "No buying power."
    elif not skip and trades_today(db_conn, exchange_name=EXCHANGE_NAME) >= MAX_TRADES_PER_DAY:
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
    scan_pairs = []
    for sym in CRYPTO_PAIRS:
        if sym in held:
            continue
        lx = last_exit_time(db_conn, sym, exchange_name=EXCHANGE_NAME)
        if lx is not None and (now - lx) < timedelta(minutes=COOLDOWN_MIN):
            continue
        scan_pairs.append(sym)

    candidates = scan_pullback_candidates(
        db_conn,
        scan_pairs,
        tickers,
        aggressive_mode=_aggressive_mode,
        price_exchange=PRICE_EXCHANGE,
        get_range_pct=get_range_pct,
        get_momentum_over=get_momentum_over,
        get_one_hour_momentum=get_one_hour_momentum,
        get_recent_high=get_recent_high,
        get_recent_low=get_recent_low,
    )
    # Trim candidate list before selection (not just for reporting)
    candidates = candidates[:_candidate_limit] if _candidate_limit < len(candidates) else candidates
    report["scanned_assets"] = [
        {k: (round(v, 3) if isinstance(v, float) else v) for k, v in c.items()}
        for c in candidates[:_candidate_limit]
    ]

    if not candidates:
        report["action_taken"] = "SKIP"
        report["details"] = "No pullback-in-uptrend setup passed the filters."
        finalize()
        return

    best = candidates[0]
    symbol = best["symbol"]
    current_price = best["price"]

    # Regime detection — compute and log, no entry gating yet (USE_REGIME_ROUTING=False)
    regime = detect_regime(db_conn, base_symbol(symbol))
    report["regime"] = regime

    # AI consult_on_entry enforcement: only take higher-conviction setups.
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
            "t3": round(best.get("t3", 0) or 0, 3),
            "t6": round(best.get("t6", 0) or 0, 3),
            "rng": round(best.get("rng", 0) or 0, 3),
            "pullback": round(best.get("pullback", 0) or 0, 3),
            "h1": round(best.get("h1", 0) or 0, 3),
            "score": round(best.get("score", 0) or 0, 3),
        }
        try:
            result = review_trade(
                symbol=symbol,
                strategy="pullback",
                signals=_sig,
                price=current_price,
                score=best.get("score", 0) or 0,
                portfolio_euro=portfolio_value,
                available_euro=cash_eur,
                open_positions=len(positions),
                db_conn=db_conn,
            )
        except Exception as e:
            # LLM unavailable — fall back to aggressive (buy directly)
            print(f"LLM review failed: {e} — buying directly", file=sys.stderr)
            result = {"verdict": "APPROVE", "reason": f"LLM unavailable: {e}", "confidence": 0}

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

    # Sizing: one concentrated, well-chosen trade. Start from ~97% of cash
    # (halved when the AI asked us to consult), then apply a volatility-adjusted
    # cap so a wide-stop (volatile) coin can't risk more than RISK_PER_TRADE_PCT
    # of equity. Tight-stop setups still deploy the full fraction.
    deploy_fraction = CONSULT_DEPLOY_FRACTION if consulting else DEPLOY_FRACTION
    order_size_eur = cash_eur * deploy_fraction

    rng_6h_best = get_range_pct(db_conn, symbol, 360, price_exchange=PRICE_EXCHANGE)
    stop_pct = min(MAX_HARD_STOP_PCT * _stop_cap_mult,
                   max(MIN_HARD_STOP_PCT, 0.5 * (rng_6h_best or MIN_HARD_STOP_PCT * 2)))
    risk_cap_eur = ((RISK_PER_TRADE_PCT * _risk_mult) / 100.0 * portfolio_value) / (stop_pct / 100.0)
    if risk_cap_eur < order_size_eur:
        order_size_eur = risk_cap_eur
    order_size_eur = min(order_size_eur, cash_eur)

    if USE_KELLY_SIZING:
        stop_price = current_price * (1 - stop_pct / 100)
        kelly_size = kelly_position_size(db_conn, EXCHANGE_NAME, current_price, stop_price, portfolio_value)
        if kelly_size > 0:
            order_size_eur = kelly_size

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
    ok_spread, sp = spread_ok(exchange, symbol, PB.MAX_SPREAD_PCT)
    if not ok_spread:
        report["action_taken"] = "SKIP"
        report["details"] = f"Spread too wide ({round(sp, 3)}% > {PB.MAX_SPREAD_PCT}%)."
        finalize()
        return

    try:
        fqty = float(exchange.amount_to_precision(symbol, qty))
        res = market_buy(exchange, symbol, fqty, current_price)
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
    # ── Orchestrator integration ──────────────────────────────────────
    # Run tracking handled by container cron_orchestrator (DB-driven)
    # ──────────────────────────────────────────────────────────────────

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

    # ── Orchestrator: schedule next run ──────────────────────────────
    # (no-op: next_run_at managed by container cron_orchestrator)


if __name__ == "__main__":
    main()
