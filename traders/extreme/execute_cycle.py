import os
import sys
import json
import time
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv

# Load environment variables
env_path = "PROJECT_ROOT/.env"
load_dotenv(dotenv_path=env_path)

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

# Headers for Alpaca
headers = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    "Content-Type": "application/json"
}

# Crypto-Only Universe (PDT Exempt, 24/7 trading)
CRYPTO_PAIRS = ["BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD", "LINK/USD"]

LOG_DIR = "PROJECT_ROOT/logs"
os.makedirs(LOG_DIR, exist_ok=True)

STATE_FILE = "PROJECT_ROOT/traders/extreme/state.json"
NOTIFY_STATE_FILE = "PROJECT_ROOT/traders/extreme/last_notify.json"

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
    return {"last_notify_time": "1970-01-01T00:00:00Z", "last_market_open": False}

def save_notify_state(state):
    try:
        with open(NOTIFY_STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"Error saving notify state: {e}", file=sys.stderr)

def log_trade(action, ticker, asset_type, signal_strength, momentum_pct, entry_price, current_price, unrealized_plpc, order_id, client_order_id, quantity, estimated_value_usd, position_size_pct, portfolio_equity, reason):
    log_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_file_path = os.path.join(LOG_DIR, f"trades-{log_date}.jsonl")
    
    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "cycle": 1,
        "action": action,
        "ticker": ticker,
        "asset_type": asset_type,
        "signal_strength": signal_strength,
        "momentum_pct": round(momentum_pct, 4) if momentum_pct else 0.0,
        "entry_price": round(entry_price, 4) if entry_price else 0.0,
        "current_price": round(current_price, 4) if current_price else 0.0,
        "unrealized_plpc": round(unrealized_plpc, 5) if unrealized_plpc else 0.0,
        "order_id": order_id,
        "client_order_id": client_order_id,
        "quantity": round(quantity, 6) if quantity else 0.0,
        "estimated_value_usd": round(estimated_value_usd, 2) if estimated_value_usd else 0.0,
        "position_size_pct": round(position_size_pct, 4) if position_size_pct else 0.0,
        "portfolio_equity_at_decision": round(portfolio_equity, 2) if portfolio_equity else 0.0,
        "reason": reason
    }
    
    with open(log_file_path, "a") as f:
        f.write(json.dumps(log_entry) + "\n")

def get_position_age_hours(symbol):
    try:
        url = f"{ALPACA_BASE_URL}/v2/orders?status=filled&symbols={symbol}&limit=10"
        response = requests.get(url, headers=headers)
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
        "positions_managed": [],
        "scanned_assets": [],
        "action_taken": "NONE",
        "details": ""
    }
    
    # Load state & notify state
    state = load_state()
    notify_state = load_notify_state()
    should_notify = False
    msg_lines = []
    
    # 1. Fetch Account Info
    acc_url = f"{ALPACA_BASE_URL}/v2/account"
    acc_res = requests.get(acc_url, headers=headers)
    if acc_res.status_code != 200:
        report["status"] = "error"
        report["details"] = f"Failed to fetch account info: {acc_res.text}"
        print(json.dumps(report))
        return
        
    account = acc_res.json()
    equity = float(account.get("equity", 0.0))
    buying_power = float(account.get("buying_power", 0.0))
    
    # 2. Fetch Clock Info (Needed for Stock market hours for SMCI/PLTR liquidation)
    clock_url = f"{ALPACA_BASE_URL}/v2/clock"
    clock_res = requests.get(clock_url, headers=headers)
    market_open = False
    if clock_res.status_code == 200:
        market_open = clock_res.json().get("is_open", False)
    
    # 3. Fetch Positions
    pos_url = f"{ALPACA_BASE_URL}/v2/positions"
    pos_res = requests.get(pos_url, headers=headers)
    positions = []
    if pos_res.status_code == 200:
        positions = pos_res.json()
        
    report["portfolio_equity"] = equity
    report["buying_power"] = buying_power
    report["open_positions_count"] = len(positions)

    # Prepare peaks state
    new_state = {}
    active_symbols = [p["symbol"] for p in positions]
    for sym in active_symbols:
        new_state[sym] = state.get(sym, {"peak_plpc": 0.0})
    
    # 4. Manage Open Positions First
    managed_any = False
    
    # Stale Position Rotation flag
    can_rotate = False
    stale_symbol = None
    stale_unrealized_plpc = 0.0
    stale_qty = 0.0
    stale_entry_price = 0.0
    stale_current_price = 0.0
    stale_age_hours = 0.0
    
    for pos in positions:
        symbol = pos["symbol"]
        unrealized_plpc = float(pos["unrealized_plpc"]) * 100.0 
        qty = float(pos["qty"])
        entry_price = float(pos["avg_entry_price"])
        current_price = float(pos["current_price"])
        asset_class = pos.get("asset_class", "us_equity")
        asset_type = "STOCK" if asset_class == "us_equity" else "CRYPTO"
        
        age_hours = get_position_age_hours(symbol)
        
        # Track peak P&L
        sym_state = new_state.get(symbol, {"peak_plpc": 0.0})
        peak_plpc = max(unrealized_plpc, sym_state.get("peak_plpc", 0.0))
        sym_state["peak_plpc"] = peak_plpc
        new_state[symbol] = sym_state
        
        sell_triggered = False
        sell_reason = ""
        
        # --- MANDATORY NON-CRYPTO OVERNIGHT LIQUIDATION ---
        # If we have any legacy stock position (like SMCI) left, we MUST close it immediately
        # (Only if market is open, or if it is crypto which can close 24/7)
        if asset_type == "STOCK":
            sell_triggered = True
            sell_reason = "Overnight Stock Clean-up (Transitioning to Crypto-Only!)"
            
        # Trailing Take Profit (TTP) - Crypto only
        elif peak_plpc >= 2.0 and unrealized_plpc <= (peak_plpc - 0.75):
            sell_triggered = True
            sell_reason = f"Trailing Take Profit hit (Peak: +{round(peak_plpc, 2)}% | Sold at: +{round(unrealized_plpc, 2)}%)"
        # Profit Lock - Crypto only
        elif peak_plpc >= 2.5 and unrealized_plpc < 2.0:
            sell_triggered = True
            sell_reason = f"Profit lock protection (Peak: +{round(peak_plpc, 2)}% | Sold at: +{round(unrealized_plpc, 2)}%)"
        # Stop-loss (-3.5%) - Crypto only
        elif unrealized_plpc <= -3.5:
            sell_triggered = True
            sell_reason = f"Stop-loss hit ({round(unrealized_plpc, 2)}% <= -3.5%)"
        # Breakeven Protection - Crypto only
        elif peak_plpc >= 1.0 and unrealized_plpc <= 0.2:
            sell_triggered = True
            sell_reason = f"Breakeven protection (Peak was +{round(peak_plpc, 2)}% | Current: +{round(unrealized_plpc, 2)}%)"
        # Time-stop (1 hour) - Crypto only
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
        
        # Check for Stale Crypto Position Rotation rule (held >30 mins, flat <1.0%)
        if asset_type == "CRYPTO" and age_hours >= 0.5 and unrealized_plpc < 1.0 and not sell_triggered:
            can_rotate = True
            stale_symbol = symbol
            stale_unrealized_plpc = unrealized_plpc
            stale_qty = qty
            stale_entry_price = entry_price
            stale_current_price = current_price
            stale_age_hours = age_hours
            pos_report["reason"] += " [FLAGGED FOR POTENTIAL ROTATION]"
        
        if sell_triggered:
            # Stocks can only close if stock market is open. Crypto can close 24/7.
            if asset_type == "CRYPTO" or market_open:
                close_url = f"{ALPACA_BASE_URL}/v2/positions/{symbol}"
                close_res = requests.delete(close_url, headers=headers)
                if close_res.status_code in [200, 201, 204]:
                    pos_report["action"] = "SELL"
                    pos_report["reason"] = sell_reason
                    managed_any = True
                    should_notify = True
                    msg_lines.append(f"🔄 **Πωλήθηκε {symbol}**: {sell_reason}")
                    log_trade(
                        action="SELL",
                        ticker=symbol,
                        asset_type=asset_type,
                        signal_strength="NO_MOMENTUM",
                        momentum_pct=0.0,
                        entry_price=entry_price,
                        current_price=current_price,
                        unrealized_plpc=float(pos["unrealized_plpc"]),
                        order_id=close_res.json().get("id") if close_res.text else None,
                        client_order_id=None,
                        quantity=qty,
                        estimated_value_usd=qty * current_price,
                        position_size_pct=0.0,
                        portfolio_equity=equity,
                        reason=sell_reason
                    )
                    if symbol in new_state:
                        del new_state[symbol]
                    if symbol == stale_symbol:
                        can_rotate = False
                else:
                    pos_report["action"] = "SELL_FAILED"
                    pos_report["reason"] = f"Failed to close position: {close_res.text}"
            else:
                pos_report["reason"] = f"{sell_reason} (Pending - Stock market closed)"
                
        report["positions_managed"].append(pos_report)

    # Save state
    save_state(new_state)

    # Refresh account and positions if we sold anything
    if managed_any:
        acc_res = requests.get(acc_url, headers=headers)
        if acc_res.status_code == 200:
            account = acc_res.json()
            buying_power = float(account.get("buying_power", 0.0))
            equity = float(account.get("equity", 0.0))
        pos_res = requests.get(pos_url, headers=headers)
        if pos_res.status_code == 200:
            positions = pos_res.json()
            
    # 5. Check open positions limit
    if len(positions) >= 5:
        report["action_taken"] = "SKIP"
        report["details"] = "Max positions limit reached (5 open positions)."
        print(json.dumps(report))
        return
        
    # If we still hold a legacy stock position (e.g., market is closed), we CANNOT buy crypto yet
    if any(p.get("asset_class") == "us_equity" for p in positions):
        report["action_taken"] = "SKIP"
        report["details"] = "Holding legacy stock position. Waiting for market open to liquidate."
        print(json.dumps(report))
        return
        
    if buying_power <= 0 and not can_rotate:
        report["action_taken"] = "SKIP"
        report["details"] = "No buying power available."
        # Log SKIP
        for pos in positions:
            symbol = pos["symbol"]
            unrealized_plpc = float(pos["unrealized_plpc"])
            qty = float(pos["qty"])
            entry_price = float(pos["avg_entry_price"])
            current_price = float(pos["current_price"])
            log_trade(
                action="SKIP",
                ticker=symbol,
                asset_type="CRYPTO",
                signal_strength="NO_MOMENTUM",
                momentum_pct=0.0,
                entry_price=entry_price,
                current_price=current_price,
                unrealized_plpc=unrealized_plpc,
                order_id=None,
                client_order_id=None,
                quantity=0.0,
                estimated_value_usd=qty * current_price,
                position_size_pct=100.0,
                portfolio_equity=equity,
                reason="No buying power — position fully deployed"
            )
        print(json.dumps(report))
        return

    # --- CRYPTO MOMENTUM SCAN & STALE POSITION ROTATION ---
    candidates = []
    
    crypto_symbols_str = ",".join(CRYPTO_PAIRS)
    crypto_url = f"https://data.alpaca.markets/v1beta3/crypto/us/snapshots?symbols={crypto_symbols_str}"
    crypto_res = requests.get(crypto_url, headers=headers)
    if crypto_res.status_code == 200:
        data = crypto_res.json().get("snapshots", {})
        for symbol, snap in data.items():
            if not snap:
                continue
            if any(p["symbol"] == symbol for p in positions):
                continue
            
            daily = snap.get("dailyBar")
            if not daily:
                continue
            open_price = float(daily.get("o", 0.0))
            if open_price <= 0:
                continue
            
            current_price = float(daily.get("c", 0.0))
            if current_price <= 0:
                continue
                
            change_pct = (current_price - open_price) / open_price * 100.0
            candidates.append({
                "symbol": symbol,
                "change_pct": change_pct,
                "current_price": current_price,
                "volume": float(daily.get("v", 0.0))
            })
            
    candidates = sorted(candidates, key=lambda x: x["change_pct"], reverse=True)
    report["scanned_assets"] = candidates[:10]
    
    signals = [c for c in candidates if c["change_pct"] >= 2.0]
    
    # Decide if we execute rotation or normal purchase
    if signals:
        best_candidate = signals[0]
        symbol = best_candidate["symbol"]
        change_pct = best_candidate["change_pct"]
        current_price = best_candidate["current_price"]
        
        # Rotation Logic: Only rotate if the new signal is stronger!
        is_rotation_execution = False
        if buying_power <= 0 and can_rotate:
            if change_pct >= 2.5:
                is_rotation_execution = True
            else:
                report["action_taken"] = "SKIP"
                report["details"] = f"Stale position {stale_symbol} flagged, but best new signal {symbol} (+{round(change_pct, 2)}%) is not strong enough to trigger rotation (needs >= 2.5%)."
                print(json.dumps(report))
                return
        
        # Position Sizing
        max_position_pct = float(os.getenv("MAX_POSITION_PCT", "0.50"))
        if change_pct >= 5.0:
            signal_strength = "EXTREME_MOMENTUM"
            sizing_mult = 1.0
        elif change_pct >= 3.0:
            signal_strength = "STRONG_MOMENTUM"
            sizing_mult = 0.67
        else:
            signal_strength = "MODERATE_MOMENTUM"
            sizing_mult = 0.33
            
        order_size_usd = equity * max_position_pct * sizing_mult
        
        # If executing rotation, we sell the stale position FIRST!
        if is_rotation_execution:
            close_url = f"{ALPACA_BASE_URL}/v2/positions/{stale_symbol}"
            close_res = requests.delete(close_url, headers=headers)
            if close_res.status_code in [200, 201, 204]:
                should_notify = True
                msg_lines.append(f"🔄 **Περιστροφή Crypto**: Πωλήθηκε το στάσιμο **{stale_symbol}** (+{round(stale_unrealized_plpc, 2)}% μετά από {round(stale_age_hours, 2)} ώρες).")
                
                log_trade(
                    action="SELL",
                    ticker=stale_symbol,
                    asset_type="CRYPTO",
                    signal_strength="NO_MOMENTUM",
                    momentum_pct=0.0,
                    entry_price=stale_entry_price,
                    current_price=stale_current_price,
                    unrealized_plpc=stale_unrealized_plpc / 100.0,
                    order_id=close_res.json().get("id") if close_res.text else None,
                    client_order_id=None,
                    quantity=stale_qty,
                    estimated_value_usd=stale_qty * stale_current_price,
                    position_size_pct=0.0,
                    portfolio_equity=equity,
                    reason=f"Stale Crypto Rotation - Sold to rotate capital into hot {symbol} (+{round(change_pct, 2)}%)."
                )
                if stale_symbol in new_state:
                    del new_state[stale_symbol]
                    save_state(new_state)
                    
                # Wait 1.5 seconds for Alpaca to update buying power
                time.sleep(1.5)
                
                # Refresh account info for updated buying power
                acc_res = requests.get(acc_url, headers=headers)
                if acc_res.status_code == 200:
                    account = acc_res.json()
                    buying_power = float(account.get("buying_power", 0.0))
            else:
                report["action_taken"] = "ROTATE_FAILED"
                report["details"] = f"Failed to close stale position {stale_symbol} to initiate rotation."
                print(json.dumps(report))
                return
        
        # Small Account Rule
        if equity < 200.0:
            order_size_usd = buying_power
            reason_rule = "small account rule (< $200 equity)"
        else:
            reason_rule = f"{signal_strength} level sizing"
            
        if order_size_usd > buying_power:
            order_size_usd = buying_power
            
        if order_size_usd >= 1.0:
            # Place BUY order
            timestamp_suffix = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            client_order_id = f"extreme-{symbol.replace('/', '')}-{timestamp_suffix}"
            
            order_data = {
                "symbol": symbol,
                "notional": str(round(order_size_usd, 2)),
                "side": "buy",
                "type": "market",
                "time_in_force": "day",
                "client_order_id": client_order_id
            }
            
            order_url = f"{ALPACA_BASE_URL}/v2/orders"
            order_res = requests.post(order_url, headers=headers, json=order_data)
            
            if order_res.status_code in [200, 201]:
                order = order_res.json()
                report["action_taken"] = "BUY"
                report["details"] = f"Successfully placed buy order for {symbol} of amount ${round(order_size_usd, 2)}."
                report["order_id"] = order.get("id")
                should_notify = True
                
                if is_rotation_execution:
                    msg_lines.append(f"🛒 **Περιστροφή Crypto**: Αγοράστηκε το νέο hot **{symbol}** (${round(order_size_usd, 2)} - {signal_strength} +{round(change_pct, 2)}%!)")
                else:
                    msg_lines.append(f"🛒 **Αγοράστηκε {symbol}** (${round(order_size_usd, 2)} - {signal_strength} +{round(change_pct, 2)}%)")
                
                log_trade(
                    action="BUY",
                    ticker=symbol,
                    asset_type="CRYPTO",
                    signal_strength=signal_strength,
                    momentum_pct=change_pct,
                    entry_price=current_price,
                    current_price=current_price,
                    unrealized_plpc=0.0,
                    order_id=order.get("id"),
                    client_order_id=client_order_id,
                    quantity=order_size_usd / current_price,
                    estimated_value_usd=order_size_usd,
                    position_size_pct=order_size_usd / equity * 100.0,
                    portfolio_equity=equity,
                    reason=f"{signal_strength} (+{round(change_pct, 2)}% intraday) on {symbol}. Deployed ${round(order_size_usd, 2)} per {reason_rule}."
                )
            else:
                report["action_taken"] = "BUY_FAILED"
                report["details"] = f"Failed to place buy order for {symbol}: {order_res.text}"
        else:
            report["action_taken"] = "SKIP"
            report["details"] = "Order size too small."
    else:
        report["action_taken"] = "SKIP"
        report["details"] = "No momentum signals found."

    # --- DECIDE HOURLY NOTIFICATION HEARTBEAT ---
    last_notify_str = notify_state.get("last_notify_time", "1970-01-01T00:00:00Z")
    last_notify_time = datetime.fromisoformat(last_notify_str.replace("Z", "+00:00"))
    now_utc = datetime.now(timezone.utc)
    seconds_since_last_notify = (now_utc - last_notify_time).total_seconds()
    
    # Heartbeat trigger (60 minutes = 3600 seconds)
    if seconds_since_last_notify >= 3600.0:
        should_notify = True
        msg_lines.insert(0, "⏱️ **Hourly Update:**")

    # If should_notify is True, construct and print the beautiful concise Greek message!
    if should_notify:
        pos_lines = []
        for p in positions:
            sym = p["symbol"]
            pl = float(p["unrealized_plpc"]) * 100.0
            peak = new_state.get(sym, {}).get("peak_plpc", 0.0)
            pos_lines.append(f"📈 **{sym}**: {round(pl, 2)}% (Peak: +{round(peak, 2)}%)")
        
        if pos_lines:
            msg_lines.extend(pos_lines)
        else:
            msg_lines.append("🔍 Καμία ανοιχτή θέση (100% Cash).")
            
        print("\n".join(msg_lines))
        notify_state["last_notify_time"] = now_utc.isoformat().replace("+00:00", "Z")
        
    save_notify_state(notify_state)

if __name__ == "__main__":
    run_cycle()
