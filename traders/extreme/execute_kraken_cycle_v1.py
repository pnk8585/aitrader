import os
import sys
import json
import time
import ccxt
from datetime import datetime, timezone
from dotenv import load_dotenv
from db_prices import (get_connection, insert_prices, get_one_hour_momentum,
                       close_connection, base_symbol,
                       load_trading_state, save_trading_state,
                       load_notify_state, save_notify_state as db_save_notify_state,
                       log_trade as db_log_trade)

# Load environment variables
ROOT_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
env_path = os.path.join(ROOT_DIR, ".env")
load_dotenv(dotenv_path=env_path)

KRAKEN_API_KEY = os.getenv("KRAKEN_API_KEY")
KRAKEN_SECRET = os.getenv("KRAKEN_SECRET")

if not KRAKEN_API_KEY or not KRAKEN_SECRET:
    print("Error: Missing Kraken credentials in .env", file=sys.stderr)
    sys.exit(1)

# Initialize Kraken via CCXT
exchange = ccxt.kraken({
    'apiKey': KRAKEN_API_KEY,
    'secret': KRAKEN_SECRET,
    'enableRateLimit': True,
})

# Universe details
CRYPTO_PAIRS = ["BTC/EUR", "ETH/EUR", "SOL/EUR", "AVAX/EUR", "LINK/EUR", "XRP/EUR", "DOGE/EUR", "SUI/EUR", "NEAR/EUR", "RENDER/EUR", "ADA/EUR", "DOT/EUR"]

# Kraken taker fee ~0.26% per side => ~0.52% round-trip. Buffer to ~0.6% to cover slippage.
ROUND_TRIP_FEE_PCT = 0.6

# Crypto-optimized ride-the-wave trailing stops (crypto is more volatile than stocks)
TTP_PEAK_PCT = 3.0          # Trailing-Take-Profit: activate after +3.0% peak
TTP_GIVEBACK_PCT = 1.0      # Sell if we give back 1.0% from peak
PLOCK_PEAK_PCT = 5.0        # Profit-Lock: activate after +5.0% peak
PLOCK_FLOOR_PCT = 3.0       # Sell if we drop below +3.0% after hitting +5.0%

LOG_DIR = os.path.join(ROOT_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

EXCHANGE_NAME = "kraken"

def log_trade(db_conn, action, ticker, signal_strength, momentum_pct, entry_price, current_price, unrealized_plpc, order_id, quantity, estimated_value_eur, position_size_pct, portfolio_equity, reason):
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
    try:
        trades = exchange.fetch_my_trades(symbol, limit=10)
        buy_trades = [t for t in trades if t['side'] == 'buy']
        if buy_trades:
            buy_trades = sorted(buy_trades, key=lambda x: x['timestamp'], reverse=True)
            latest_buy = buy_trades[0]
            return float(latest_buy['price']), latest_buy['datetime']
    except Exception as e:
        print(f"Error fetching trades for {symbol}: {e}", file=sys.stderr)
    return current_price, datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def run_cycle():
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "success",
        "positions_managed": [],
        "scanned_assets": [],
        "action_taken": "NONE",
        "details": ""
    }
    
    # Kraken is the sole writer of the shared price DB. Open once per cycle and
    # close on every exit path inside finalize().
    db_conn = get_connection()
    
    state = load_trading_state(db_conn, EXCHANGE_NAME)
    notify_state = load_notify_state(db_conn, EXCHANGE_NAME)
    should_notify = False
    msg_lines = []

    # Heartbeat + notify emitter. Called at every exit path so hourly updates are
    # never skipped by an early return (rotation-skip, rotate-failed, etc.).
    def finalize():
        nonlocal should_notify
        last_notify_str = notify_state.get("last_notify_time", "1970-01-01T00:00:00Z")
        last_notify_time = datetime.fromisoformat(last_notify_str.replace("Z", "+00:00"))
        now_utc = datetime.now(timezone.utc)
        seconds_since_last_notify = (now_utc - last_notify_time).total_seconds()
        if seconds_since_last_notify >= 3600.0:
            should_notify = True
            msg_lines.insert(0, "⏱️ **Kraken Hourly Update:**")
        if should_notify:
            pos_lines = []
            for p in positions:
                sym = p["symbol"]
                sym_state = new_state.get(sym, {})
                ent_price = sym_state.get("entry_price", p["current_price"])
                cur_price = p["current_price"]
                pl = (cur_price - ent_price) / ent_price * 100.0
                peak = sym_state.get("peak_plpc", 0.0)
                pos_lines.append(f"📈 **{sym} (Kraken)**: {round(pl, 2)}% (Peak: +{round(peak, 2)}%)")
            if pos_lines:
                msg_lines.extend(pos_lines)
            else:
                msg_lines.append("🔍 Καμία ανοιχτή θέση στο Kraken (100% Cash).")
            print("\n".join(msg_lines))
            notify_state["last_notify_time"] = now_utc.isoformat().replace("+00:00", "Z")
        db_save_notify_state(db_conn, EXCHANGE_NAME, notify_state)
        close_connection(db_conn)

    # 1. Fetch Tickers and Balances
    try:
        tickers = exchange.fetch_tickers(CRYPTO_PAIRS)
        # Kraken is the sole writer: persist current prices BEFORE any skip/branch
        # logic so the shared 1h-momentum history is always fed.
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
    
    # Identify open positions (any coin balance with value > 1.0 EUR)
    for sym in CRYPTO_PAIRS:
        coin = sym.split('/')[0]
        qty = balance['total'].get(coin, 0.0)
        ticker = tickers.get(sym)
        if not ticker:
            continue
        price = ticker['last']
        value_eur = qty * price
        
        if value_eur > 1.0: # 1 EUR dust limit
            portfolio_value += value_eur
            positions.append({
                'symbol': sym,
                'coin': coin,
                'qty': qty,
                'current_price': price,
                'value_eur': value_eur,
                'percentage': ticker['percentage']
            })
            
    report["portfolio_equity"] = portfolio_value
    report["buying_power"] = cash_eur
    report["open_positions_count"] = len(positions)

    # Initialize/Clean up local peaks state
    new_state = {}
    for pos in positions:
        sym = pos['symbol']
        new_state[sym] = state.get(sym, {})
        if "entry_price" not in new_state[sym]:
            # Auto-detect entry price from trade history
            entry_price, entry_time = get_entry_price_and_time(sym, pos['current_price'])
            new_state[sym]["entry_price"] = entry_price
            new_state[sym]["entry_time"] = entry_time
        if "peak_plpc" not in new_state[sym]:
            new_state[sym]["peak_plpc"] = 0.0
            
    # 2. Manage Open Positions First
    managed_any = False
    can_rotate = False
    stale_symbol = None
    stale_unrealized_plpc = 0.0
    stale_qty = 0.0
    stale_entry_price = 0.0
    stale_current_price = 0.0
    stale_age_hours = 0.0
    
    for pos in positions:
        symbol = pos["symbol"]
        qty = pos["qty"]
        current_price = pos["current_price"]
        
        sym_state = new_state[symbol]
        entry_price = sym_state["entry_price"]
        entry_time_str = sym_state["entry_time"]
        
        # Calculate Position Age
        entry_time = datetime.fromisoformat(entry_time_str.replace('Z', '+00:00'))
        now = datetime.now(timezone.utc)
        age_hours = (now - entry_time).total_seconds() / 3600.0
        
        # Calculate unrealized P&L%
        unrealized_plpc = (current_price - entry_price) / entry_price * 100.0
        peak_plpc = max(unrealized_plpc, sym_state.get("peak_plpc", 0.0))
        sym_state["peak_plpc"] = peak_plpc
        new_state[symbol] = sym_state
        
        sell_triggered = False
        sell_reason = ""
        
        # Trailing Take Profit (TTP)
        if peak_plpc >= TTP_PEAK_PCT and unrealized_plpc <= (peak_plpc - TTP_GIVEBACK_PCT):
            sell_triggered = True
            sell_reason = f"Trailing Take Profit hit (Peak: +{round(peak_plpc, 2)}% | Sold: +{round(unrealized_plpc, 2)}%)"
        # Profit Lock
        elif peak_plpc >= PLOCK_PEAK_PCT and unrealized_plpc < PLOCK_FLOOR_PCT:
            sell_triggered = True
            sell_reason = f"Profit lock protection (Peak: +{round(peak_plpc, 2)}% | Sold: +{round(unrealized_plpc, 2)}%)"
        # Stop-loss (-3.5%)
        elif unrealized_plpc <= -3.5:
            sell_triggered = True
            sell_reason = f"Stop-loss hit ({round(unrealized_plpc, 2)}% <= -3.5%)"
        # Breakeven Protection (exit above fee floor so we lock NET-positive, not a fee-loss)
        elif peak_plpc >= 1.0 and unrealized_plpc <= ROUND_TRIP_FEE_PCT:
            sell_triggered = True
            sell_reason = f"Breakeven protection (Peak was +{round(peak_plpc, 2)}% | Current: +{round(unrealized_plpc, 2)}% | fee floor +{ROUND_TRIP_FEE_PCT}%)"
            
        pos_report = {
            "symbol": symbol,
            "unrealized_plpc": unrealized_plpc,
            "age_hours": age_hours,
            "action": "HOLD",
            "reason": f"Within limits (peak: +{round(peak_plpc, 2)}%)" if not sell_triggered else sell_reason
        }
        
        # Stale Crypto Position Rotation rule (held >30 mins flat <1.0%, or held >1 hour).
        # We only sell stale/time-stopped positions if we have found a new momentum target to buy.
        is_stale = (age_hours >= 0.5 and unrealized_plpc < 1.0) or (age_hours >= 1.0)
        if is_stale and not sell_triggered and not can_rotate:
            can_rotate = True
            stale_symbol = symbol
            stale_unrealized_plpc = unrealized_plpc
            stale_qty = qty
            stale_entry_price = entry_price
            stale_current_price = current_price
            stale_age_hours = age_hours
            pos_report["reason"] += " [FLAGGED FOR POTENTIAL ROTATION]"
            
        if sell_triggered:
            try:
                # Format quantity with Kraken precision
                exchange.load_markets()
                formatted_qty = float(exchange.amount_to_precision(symbol, qty))
                close_res = exchange.create_market_sell_order(symbol, formatted_qty)
                
                pos_report["action"] = "SELL"
                pos_report["reason"] = sell_reason
                managed_any = True
                should_notify = True
                msg_lines.append(f"🔄 **Πωλήθηκε {symbol} (Kraken)**: {sell_reason}")
                
                log_trade(db_conn,
                    action="SELL",
                    ticker=symbol,
                    signal_strength="NO_MOMENTUM",
                    momentum_pct=0.0,
                    entry_price=entry_price,
                    current_price=current_price,
                    unrealized_plpc=unrealized_plpc / 100.0,
                    order_id=close_res.get("id"),
                    quantity=qty,
                    estimated_value_eur=qty * current_price,
                    position_size_pct=0.0,
                    portfolio_equity=portfolio_value,
                    reason=sell_reason
                )
                if symbol in new_state:
                    del new_state[symbol]
                positions = [p for p in positions if p["symbol"] != symbol]
                if symbol == stale_symbol:
                    can_rotate = False
            except Exception as e:
                pos_report["action"] = "SELL_FAILED"
                pos_report["reason"] = f"Failed to sell: {e}"
                print(f"Error selling {symbol} on Kraken: {e}", file=sys.stderr)
                
        report["positions_managed"].append(pos_report)
        
    save_trading_state(db_conn, EXCHANGE_NAME, new_state)
    
    # Refresh balances if any action taken
    if managed_any:
        try:
            balance = exchange.fetch_balance()
            cash_eur = balance['total'].get('EUR', 0.0)
        except:
            pass
            
    # Check open positions limit
    skip_buying = False
    skip_reason = ""
    if len(positions) >= 5:
        report["action_taken"] = "SKIP"
        report["details"] = "Max positions limit reached (5 open positions)."
        skip_buying = True
        skip_reason = "Max positions limit reached (5 open positions)."
        
    elif cash_eur <= 0.45 and not can_rotate: # Minimum Kraken trade is 0.45 EUR
        report["action_taken"] = "SKIP"
        report["details"] = "No buying power available."
        skip_buying = True
        skip_reason = "No buying power available."

    if not skip_buying:
        # 3. Momentum Scan & Rotation
        candidates = []
        for sym in CRYPTO_PAIRS:
            if any(p["symbol"] == sym for p in positions):
                continue
            ticker = tickers.get(sym)
            if not ticker:
                continue
            change_pct = ticker.get('percentage')
            if change_pct is None:
                open_p = ticker.get('open')
                last_p = ticker.get('last')
                if open_p and open_p > 0 and last_p:
                    change_pct = (last_p - open_p) / open_p * 100.0
                else:
                    change_pct = 0.0
            hourly_change_pct = get_one_hour_momentum(db_conn, sym)
            candidates.append({
                "symbol": sym,
                "change_pct": change_pct,
                "hourly_change_pct": hourly_change_pct,
                "current_price": ticker['last']
            })

        # Sort by the strongest of daily vs hourly momentum.
        candidates = sorted(
            candidates,
            key=lambda x: max(x["change_pct"], x["hourly_change_pct"] if x["hourly_change_pct"] is not None else -999.0),
            reverse=True,
        )
        report["scanned_assets"] = candidates[:10]

        signals = [
            c for c in candidates
            if c["change_pct"] >= 2.0
            or (c["hourly_change_pct"] is not None and c["hourly_change_pct"] >= 1.5)
        ]
    
        if signals:
            best_candidate = signals[0]
            symbol = best_candidate["symbol"]
            change_pct = best_candidate["change_pct"]
            hourly_change_pct = best_candidate["hourly_change_pct"]
            current_price = best_candidate["current_price"]

            is_rotation_execution = False
            if cash_eur <= 0.45 and can_rotate:
                if change_pct >= 2.5 or (hourly_change_pct is not None and hourly_change_pct >= 2.0):
                    is_rotation_execution = True
                else:
                    report["action_taken"] = "SKIP"
                    report["details"] = f"Stale position {stale_symbol} flagged, but new signal {symbol} is not strong enough to trigger rotation (needs daily >= 2.5% or hourly >= 2.0%)."
                    finalize()
                    return

            # Sizing and Signal Strength: evaluate daily and hourly tiers
            # independently, then keep whichever yields the larger size.
            max_position_pct = 0.50

            # Daily tiers
            if change_pct >= 5.0:
                daily_strength = "EXTREME_MOMENTUM"
                daily_mult = 1.0
            elif change_pct >= 3.0:
                daily_strength = "STRONG_MOMENTUM"
                daily_mult = 0.67
            elif change_pct >= 2.0:
                daily_strength = "MODERATE_MOMENTUM"
                daily_mult = 0.33
            else:
                daily_strength = None
                daily_mult = 0.0

            # Hourly tiers
            h = hourly_change_pct if hourly_change_pct is not None else -999.0
            if h >= 3.0:
                hourly_strength = "EXTREME_MOMENTUM"
                hourly_mult = 1.0
            elif h >= 2.0:
                hourly_strength = "STRONG_MOMENTUM"
                hourly_mult = 0.67
            elif h >= 1.5:
                hourly_strength = "MODERATE_MOMENTUM"
                hourly_mult = 0.33
            else:
                hourly_strength = None
                hourly_mult = 0.0

            # Choose the stronger of the two tiers.
            if hourly_mult > daily_mult:
                signal_strength = hourly_strength
                sizing_mult = hourly_mult
                hourly_is_primary = True
            else:
                signal_strength = daily_strength if daily_strength is not None else hourly_strength
                sizing_mult = daily_mult if daily_mult > 0.0 else hourly_mult
                hourly_is_primary = False

            order_size_eur = portfolio_value * max_position_pct * sizing_mult

            # Human-readable momentum tag, noting the hourly driver when it wins.
            if hourly_is_primary:
                momentum_desc = f"{signal_strength} (+{round(hourly_change_pct, 2)}% 1h)"
            else:
                momentum_desc = f"{signal_strength} (+{round(change_pct, 2)}% intraday)"

            # If executing rotation, we sell the stale position FIRST!
            if is_rotation_execution:
                try:
                    exchange.load_markets()
                    formatted_qty = float(exchange.amount_to_precision(stale_symbol, stale_qty))
                    close_res = exchange.create_market_sell_order(stale_symbol, formatted_qty)
                
                    should_notify = True
                    msg_lines.append(f"🔄 **Περιστροφή Crypto (Kraken)**: Πωλήθηκε το στάσιμο **{stale_symbol}** (+{round(stale_unrealized_plpc, 2)}% μετά από {round(stale_age_hours, 2)} ώρες).")
                
                    log_trade(db_conn,
                        action="SELL",
                        ticker=stale_symbol,
                        signal_strength="NO_MOMENTUM",
                        momentum_pct=0.0,
                        entry_price=stale_entry_price,
                        current_price=stale_current_price,
                        unrealized_plpc=stale_unrealized_plpc / 100.0,
                        order_id=close_res.get("id"),
                        quantity=stale_qty,
                        estimated_value_eur=stale_qty * stale_current_price,
                        position_size_pct=0.0,
                        portfolio_equity=portfolio_value,
                        reason=f"Stale Crypto Rotation - Sold to rotate capital into hot {symbol} (+{round(change_pct, 2)}%)."
                    )
                    if stale_symbol in new_state:
                        del new_state[stale_symbol]
                        save_trading_state(db_conn, EXCHANGE_NAME, new_state)
                    positions = [p for p in positions if p["symbol"] != stale_symbol]
                    
                    time.sleep(1.5) # Wait for Kraken updates
                    balance = exchange.fetch_balance()
                    cash_eur = balance['total'].get('EUR', 0.0)
                except Exception as e:
                    report["action_taken"] = "ROTATE_FAILED"
                    report["details"] = f"Failed to close stale position {stale_symbol} for rotation: {e}"
                    print(f"Rotation sell failed on Kraken: {e}", file=sys.stderr)
                    finalize()
                    return
                
            # Small Account Rule (< 200 EUR equity)
            if portfolio_value < 200.0:
                order_size_eur = cash_eur
                reason_rule = "small account rule (< 200 EUR equity)"
            else:
                reason_rule = f"{signal_strength} level sizing"
            
            if order_size_eur > cash_eur:
                order_size_eur = cash_eur
            
            if order_size_eur >= 0.45: # Minimum trade limit
                qty = order_size_eur / current_price
                try:
                    exchange.load_markets()
                    formatted_qty = float(exchange.amount_to_precision(symbol, qty))
                    order_res = exchange.create_market_buy_order(symbol, formatted_qty)
                
                    report["action_taken"] = "BUY"
                    report["details"] = f"Successfully placed buy order for {symbol} of amount EUR {round(order_size_eur, 2)}."
                    should_notify = True
                
                    if is_rotation_execution:
                        msg_lines.append(f"🛒 **Περιστροφή Crypto (Kraken)**: Αγοράστηκε το νέο hot **{symbol}** (EUR {round(order_size_eur, 2)} - {momentum_desc}!)")
                    else:
                        msg_lines.append(f"🛒 **Αγοράστηκε {symbol} (Kraken)** (EUR {round(order_size_eur, 2)} - {momentum_desc})")
                
                    new_state[symbol] = {
                        "entry_price": current_price,
                        "entry_time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        "peak_plpc": 0.0
                    }
                    save_trading_state(db_conn, EXCHANGE_NAME, new_state)
                    positions.append({
                        "symbol": symbol,
                        "current_price": current_price
                    })
                
                    log_trade(db_conn,
                        action="BUY",
                        ticker=symbol,
                        signal_strength=signal_strength,
                        momentum_pct=change_pct,
                        entry_price=current_price,
                        current_price=current_price,
                        unrealized_plpc=0.0,
                        order_id=order_res.get("id"),
                        quantity=formatted_qty,
                        estimated_value_eur=order_size_eur,
                        position_size_pct=order_size_eur / portfolio_value * 100.0,
                        portfolio_equity=portfolio_value,
                        reason=f"{momentum_desc} on {symbol}. Deployed EUR {round(order_size_eur, 2)} per {reason_rule}."
                    )
                except Exception as e:
                    report["action_taken"] = "BUY_FAILED"
                    report["details"] = f"Failed to place buy order for {symbol}: {e}"
                    print(f"Buy failed on Kraken: {e}", file=sys.stderr)
            else:
                report["action_taken"] = "SKIP"
                report["details"] = "Order size too small."
        else:
            report["action_taken"] = "SKIP"
            report["details"] = "No momentum signals found."
        
    # 4. Hourly Notification Heartbeat + Notify
    finalize()

if __name__ == "__main__":
    run_cycle()
