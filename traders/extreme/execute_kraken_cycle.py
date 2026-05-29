import os
import sys
import json
import time
import ccxt
from datetime import datetime, timezone
from dotenv import load_dotenv

# Load environment variables
env_path = "PROJECT_ROOT/.env"
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

LOG_DIR = "PROJECT_ROOT/logs"
os.makedirs(LOG_DIR, exist_ok=True)

STATE_FILE = "PROJECT_ROOT/traders/extreme/kraken_state.json"
NOTIFY_STATE_FILE = "PROJECT_ROOT/traders/extreme/kraken_last_notify.json"

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {}

def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"Error saving state: {e}", file=sys.stderr)

def load_notify_state():
    if os.path.exists(NOTIFY_STATE_FILE):
        try:
            with open(NOTIFY_STATE_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {"last_notify_time": "1970-01-01T00:00:00Z"}

def save_notify_state(state):
    try:
        with open(NOTIFY_STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"Error saving notify state: {e}", file=sys.stderr)

def log_trade(action, ticker, signal_strength, momentum_pct, entry_price, current_price, unrealized_plpc, order_id, quantity, estimated_value_eur, position_size_pct, portfolio_equity, reason):
    log_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_file_path = os.path.join(LOG_DIR, f"kraken_trades-{log_date}.jsonl")
    
    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "cycle": 1,
        "action": action,
        "ticker": ticker,
        "signal_strength": signal_strength,
        "momentum_pct": round(momentum_pct, 4) if momentum_pct else 0.0,
        "entry_price": round(entry_price, 4) if entry_price else 0.0,
        "current_price": round(current_price, 4) if current_price else 0.0,
        "unrealized_plpc": round(unrealized_plpc, 5) if unrealized_plpc else 0.0,
        "order_id": order_id,
        "quantity": round(quantity, 6) if quantity else 0.0,
        "estimated_value_eur": round(estimated_value_eur, 2) if estimated_value_eur else 0.0,
        "position_size_pct": round(position_size_pct, 4) if position_size_pct else 0.0,
        "portfolio_equity_at_decision": round(portfolio_equity, 2) if portfolio_equity else 0.0,
        "reason": reason
    }
    
    with open(log_file_path, "a") as f:
        f.write(json.dumps(log_entry) + "\n")

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
    
    state = load_state()
    notify_state = load_notify_state()
    should_notify = False
    msg_lines = []
    
    # 1. Fetch Tickers and Balances
    try:
        tickers = exchange.fetch_tickers(CRYPTO_PAIRS)
        balance = exchange.fetch_balance()
    except Exception as e:
        report["status"] = "error"
        report["details"] = f"Failed to fetch market/account data: {e}"
        print(json.dumps(report), file=sys.stderr)
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
        if peak_plpc >= 2.0 and unrealized_plpc <= (peak_plpc - 0.75):
            sell_triggered = True
            sell_reason = f"Trailing Take Profit hit (Peak: +{round(peak_plpc, 2)}% | Sold: +{round(unrealized_plpc, 2)}%)"
        # Profit Lock
        elif peak_plpc >= 2.5 and unrealized_plpc < 2.0:
            sell_triggered = True
            sell_reason = f"Profit lock protection (Peak: +{round(peak_plpc, 2)}% | Sold: +{round(unrealized_plpc, 2)}%)"
        # Stop-loss (-3.5%)
        elif unrealized_plpc <= -3.5:
            sell_triggered = True
            sell_reason = f"Stop-loss hit ({round(unrealized_plpc, 2)}% <= -3.5%)"
        # Breakeven Protection
        elif peak_plpc >= 1.0 and unrealized_plpc <= 0.2:
            sell_triggered = True
            sell_reason = f"Breakeven protection (Peak was +{round(peak_plpc, 2)}% | Current: +{round(unrealized_plpc, 2)}%)"
        # Time-stop (1 hour)
        elif age_hours > 1.0:
            sell_triggered = True
            sell_reason = f"Held > 1 hour stale position (age: {round(age_hours, 2)} hours)"
            
        pos_report = {
            "symbol": symbol,
            "unrealized_plpc": unrealized_plpc,
            "age_hours": age_hours,
            "action": "HOLD",
            "reason": f"Within limits (peak: +{round(peak_plpc, 2)}%)" if not sell_triggered else sell_reason
        }
        
        # Stale Crypto Position Rotation rule (held >30 mins, flat <1.0%)
        if age_hours >= 0.5 and unrealized_plpc < 1.0 and not sell_triggered:
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
                
                log_trade(
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
                if symbol == stale_symbol:
                    can_rotate = False
            except Exception as e:
                pos_report["action"] = "SELL_FAILED"
                pos_report["reason"] = f"Failed to sell: {e}"
                print(f"Error selling {symbol} on Kraken: {e}", file=sys.stderr)
                
        report["positions_managed"].append(pos_report)
        
    save_state(new_state)
    
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
    # Check open positions limit
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
        # Log SKIP
        for pos in positions:
            symbol = pos["symbol"]
            sym_state = new_state.get(symbol, {})
            log_trade(
                action="SKIP",
                ticker=symbol,
                signal_strength="NO_MOMENTUM",
                momentum_pct=0.0,
                entry_price=sym_state.get("entry_price", pos["current_price"]),
                current_price=pos["current_price"],
                unrealized_plpc=pos["value_eur"] / portfolio_value,
                order_id=None,
                quantity=0.0,
                estimated_value_eur=pos["value_eur"],
                position_size_pct=100.0,
                portfolio_equity=portfolio_value,
                reason="No buying power — position fully deployed"
            )

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
            candidates.append({
                "symbol": sym,
                "change_pct": change_pct,
                "current_price": ticker['last']
            })
        
        candidates = sorted(candidates, key=lambda x: x["change_pct"], reverse=True)
        report["scanned_assets"] = candidates[:10]
    
        signals = [c for c in candidates if c["change_pct"] >= 2.0]
    
        if signals:
            best_candidate = signals[0]
            symbol = best_candidate["symbol"]
            change_pct = best_candidate["change_pct"]
            current_price = best_candidate["current_price"]
        
            is_rotation_execution = False
            if cash_eur <= 0.45 and can_rotate:
                if change_pct >= 2.5:
                    is_rotation_execution = True
                else:
                    report["action_taken"] = "SKIP"
                    report["details"] = f"Stale position {stale_symbol} flagged, but new signal {symbol} is not strong enough to trigger rotation (needs >= 2.5%)."
                    return
                
            # Sizing and Signal Strength
            max_position_pct = 0.50
            if change_pct >= 5.0:
                signal_strength = "EXTREME_MOMENTUM"
                sizing_mult = 1.0
            elif change_pct >= 3.0:
                signal_strength = "STRONG_MOMENTUM"
                sizing_mult = 0.67
            else:
                signal_strength = "MODERATE_MOMENTUM"
                sizing_mult = 0.33
            
            order_size_eur = portfolio_value * max_position_pct * sizing_mult
        
            # If executing rotation, we sell the stale position FIRST!
            if is_rotation_execution:
                try:
                    exchange.load_markets()
                    formatted_qty = float(exchange.amount_to_precision(stale_symbol, stale_qty))
                    close_res = exchange.create_market_sell_order(stale_symbol, formatted_qty)
                
                    should_notify = True
                    msg_lines.append(f"🔄 **Περιστροφή Crypto (Kraken)**: Πωλήθηκε το στάσιμο **{stale_symbol}** (+{round(stale_unrealized_plpc, 2)}% μετά από {round(stale_age_hours, 2)} ώρες).")
                
                    log_trade(
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
                        save_state(new_state)
                    
                    time.sleep(1.5) # Wait for Kraken updates
                    balance = exchange.fetch_balance()
                    cash_eur = balance['total'].get('EUR', 0.0)
                except Exception as e:
                    report["action_taken"] = "ROTATE_FAILED"
                    report["details"] = f"Failed to close stale position {stale_symbol} for rotation: {e}"
                    print(f"Rotation sell failed on Kraken: {e}", file=sys.stderr)
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
                        msg_lines.append(f"🛒 **Περιστροφή Crypto (Kraken)**: Αγοράστηκε το νέο hot **{symbol}** (EUR {round(order_size_eur, 2)} - {signal_strength} +{round(change_pct, 2)}%!)")
                    else:
                        msg_lines.append(f"🛒 **Αγοράστηκε {symbol} (Kraken)** (EUR {round(order_size_eur, 2)} - {signal_strength} +{round(change_pct, 2)}%)")
                
                    new_state[symbol] = {
                        "entry_price": current_price,
                        "entry_time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        "peak_plpc": 0.0
                    }
                    save_state(new_state)
                
                    log_trade(
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
                        reason=f"{signal_strength} (+{round(change_pct, 2)}% intraday) on {symbol}. Deployed EUR {round(order_size_eur, 2)} per {reason_rule}."
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
        
    # 4. Decide Hourly Notification Heartbeat
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
        
    save_notify_state(notify_state)

if __name__ == "__main__":
    run_cycle()
