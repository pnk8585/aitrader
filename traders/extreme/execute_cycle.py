import os
import sys
import json
import time
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv
from db_prices import (get_connection, get_one_hour_momentum,
                       close_connection, base_symbol,
                       load_trading_state, save_trading_state,
                       load_notify_state, save_notify_state as db_save_notify_state,
                       log_trade as db_log_trade)

# Load environment variables
env_path = "PROJECT_ROOT/.env"
load_dotenv(dotenv_path=env_path)

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL = "https://api.alpaca.markets"

# Headers for Alpaca
headers = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    "Content-Type": "application/json"
}

# Crypto-Only Universe (PDT Exempt, 24/7 trading)
CRYPTO_PAIRS = ["BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD", "LINK/USD"]

# Alpaca crypto fee ~0.15-0.25% per side => ~0.5% round-trip. Buffer to ~0.5% to cover slippage.
ROUND_TRIP_FEE_PCT = 0.5

# Crypto-optimized ride-the-wave trailing stops (crypto is more volatile than stocks)
TTP_PEAK_PCT = 3.0          # Trailing-Take-Profit: activate after +3.0% peak
TTP_GIVEBACK_PCT = 1.0      # Sell if we give back 1.0% from peak
PLOCK_PEAK_PCT = 5.0        # Profit-Lock: activate after +5.0% peak
PLOCK_FLOOR_PCT = 3.0       # Sell if we drop below +3.0% after hitting +5.0%

LOG_DIR = "PROJECT_ROOT/logs"
os.makedirs(LOG_DIR, exist_ok=True)

EXCHANGE_NAME = "alpaca"

def log_trade(db_conn, action, ticker, asset_type, signal_strength, momentum_pct, entry_price, current_price, unrealized_plpc, order_id, client_order_id, quantity, estimated_value_usd, position_size_pct, portfolio_equity, reason):
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
    )

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
        "positions_managed": [],
        "scanned_assets": [],
        "action_taken": "NONE",
        "details": ""
    }

    # Alpaca is read-only: open connection for hourly momentum queries only.
    # Kraken writes the prices; Alpaca reads them.
    db_conn = get_connection()

    # Load state & notify state
    state = load_trading_state(db_conn, EXCHANGE_NAME)
    notify_state = load_notify_state(db_conn, EXCHANGE_NAME)
    if "last_market_open" not in notify_state:
        notify_state["last_market_open"] = False
    should_notify = False
    msg_lines = []

    # Heartbeat + notify emitter. Called at every exit path so hourly updates are
    # never skipped by an early return (rotation-skip, rotate-failed, etc.).
    def finalize():
        nonlocal should_notify
        action = report.get("action_taken", "")
        is_error = report.get("status") == "error"
        has_trade = action in ("BUY", "SELL")
        has_fail = action in ("BUY_FAILED", "SELL_FAILED")

        last_notify_str = notify_state.get("last_notify_time", "1970-01-01T00:00:00Z")
        last_notify_time = datetime.fromisoformat(last_notify_str.replace("Z", "+00:00"))
        now_utc = datetime.now(timezone.utc)
        hourly = (now_utc - last_notify_time).total_seconds() >= 3600.0

        if is_error:
            print(f"❌ Alpaca: {report.get('details', 'unknown error')}")
        elif has_trade:
            for line in msg_lines:
                if line.startswith(("🛒", "🔄")):
                    print(line)
                    break
        elif has_fail:
            print(f"⚠️ Alpaca {action}: {report.get('details', '')}")
        elif hourly:
            pos_lines = []
            for p in positions:
                sym = p["symbol"]
                pl = float(p["unrealized_plpc"]) * 100.0
                pos_lines.append(f"{sym.split('/')[0]} {round(pl,1)}%")
            pos_str = " | ".join(pos_lines) if pos_lines else "c"
            print(f"💰 Alpaca: {round(equity,2)}$ · {len(positions)} pos · {pos_str}")
            should_notify = True
            notify_state["last_notify_time"] = now_utc.isoformat().replace("+00:00", "Z")
        # else silent

        db_save_notify_state(db_conn, EXCHANGE_NAME, notify_state)
        close_connection(db_conn)

    # 1. Fetch Account Info
    acc_url = f"{ALPACA_BASE_URL}/v2/account"
    acc_res = requests.get(acc_url, headers=headers, timeout=10)
    if acc_res.status_code != 200:
        report["status"] = "error"
        report["details"] = f"Failed to fetch account info: {acc_res.text}"
        pass # print(json.dumps(report))
        return
        
    account = acc_res.json()
    equity = float(account.get("equity", 0.0))
    buying_power = float(account.get("buying_power", 0.0))
    
    # 2. Fetch Clock Info (Needed for Stock market hours for SMCI/PLTR liquidation)
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
        elif peak_plpc >= TTP_PEAK_PCT and unrealized_plpc <= (peak_plpc - TTP_GIVEBACK_PCT):
            sell_triggered = True
            sell_reason = f"Trailing Take Profit hit (Peak: +{round(peak_plpc, 2)}% | Sold at: +{round(unrealized_plpc, 2)}%)"
        # Profit Lock - Crypto only
        elif peak_plpc >= PLOCK_PEAK_PCT and unrealized_plpc < PLOCK_FLOOR_PCT:
            sell_triggered = True
            sell_reason = f"Profit lock protection (Peak: +{round(peak_plpc, 2)}% | Sold at: +{round(unrealized_plpc, 2)}%)"
        # Stop-loss (-3.5%) - Crypto only
        elif unrealized_plpc <= -3.5:
            sell_triggered = True
            sell_reason = f"Stop-loss hit ({round(unrealized_plpc, 2)}% <= -3.5%)"
        # Breakeven Protection - Crypto only (exit above fee floor so we lock NET-positive)
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
        
        # Check for Stale Crypto Position Rotation rule (held >30 mins flat <1.0%, or held >1 hour).
        # We only sell stale/time-stopped positions if we have found a new momentum target to buy.
        is_stale = asset_type == "CRYPTO" and ((age_hours >= 0.5 and unrealized_plpc < 1.0) or (age_hours >= 1.0))
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
            # Stocks can only close if stock market is open. Crypto can close 24/7.
            if asset_type == "CRYPTO" or market_open:
                close_url = f"{ALPACA_BASE_URL}/v2/positions/{symbol}"
                close_res = requests.delete(close_url, headers=headers, timeout=10)
                if close_res.status_code in [200, 201, 204]:
                    pos_report["action"] = "SELL"
                    pos_report["reason"] = sell_reason
                    managed_any = True
                    should_notify = True
                    msg_lines.append(f"🔄 **Πωλήθηκε {symbol}**: {sell_reason}")
                    log_trade(db_conn,
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
                    positions = [p for p in positions if p["symbol"] != symbol]
                    if symbol == stale_symbol:
                        can_rotate = False
                else:
                    pos_report["action"] = "SELL_FAILED"
                    pos_report["reason"] = f"Failed to close position: {close_res.text}"
            else:
                pos_report["reason"] = f"{sell_reason} (Pending - Stock market closed)"
                
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
            
    # 5. Check open positions limit
    skip_buying = False
    skip_reason = ""
    if len(positions) >= 5:
        report["action_taken"] = "SKIP"
        report["details"] = "Max positions limit reached (5 open positions)."
        skip_buying = True
        skip_reason = "Max positions limit reached (5 open positions)."
        
    # If we still hold a legacy stock position (e.g., market is closed), we CANNOT buy crypto yet
    elif any(p.get("asset_class") == "us_equity" for p in positions):
        report["action_taken"] = "SKIP"
        report["details"] = "Holding legacy stock position. Waiting for market open to liquidate."
        skip_buying = True
        skip_reason = "Holding legacy stock position. Waiting for market open to liquidate."
        
    elif buying_power <= 0 and not can_rotate:
        report["action_taken"] = "SKIP"
        report["details"] = "No buying power available."
        skip_buying = True
        skip_reason = "No buying power available."
        # Log SKIP
        for pos in positions:
            symbol = pos["symbol"]
            unrealized_plpc = float(pos["unrealized_plpc"])
            qty = float(pos["qty"])
            entry_price = float(pos["avg_entry_price"])
            current_price = float(pos["current_price"])
    if not skip_buying:
        # --- CRYPTO MOMENTUM SCAN & STALE POSITION ROTATION ---
        candidates = []

        crypto_symbols_str = ",".join(CRYPTO_PAIRS)
        crypto_url = f"https://data.alpaca.markets/v1beta3/crypto/us/snapshots?symbols={crypto_symbols_str}"
        crypto_res = requests.get(crypto_url, headers=headers, timeout=10)
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

                # Query hourly momentum from shared Kraken-written DB.
                # Alpaca uses base symbols like 'BTC', matching the DB keys.
                hourly_change_pct = get_one_hour_momentum(db_conn, base_symbol(symbol))

                candidates.append({
                    "symbol": symbol,
                    "change_pct": change_pct,
                    "hourly_change_pct": hourly_change_pct,
                    "current_price": current_price,
                    "volume": float(daily.get("v", 0.0))
                })

        # Sort by the strongest of daily vs hourly momentum.
        candidates = sorted(
            candidates,
            key=lambda x: max(x["change_pct"], x["hourly_change_pct"] if x["hourly_change_pct"] is not None else -999.0),
            reverse=True,
        )
        report["scanned_assets"] = candidates[:10]

        # Candidate qualification: daily >= 2.0 OR hourly >= 1.5
        signals = [
            c for c in candidates
            if c["change_pct"] >= 2.0
            or (c["hourly_change_pct"] is not None and c["hourly_change_pct"] >= 1.5)
        ]
    
        # Decide if we execute rotation or normal purchase
        if signals:
            best_candidate = signals[0]
            symbol = best_candidate["symbol"]
            change_pct = best_candidate["change_pct"]
            hourly_change_pct = best_candidate["hourly_change_pct"]
            current_price = best_candidate["current_price"]

            # Rotation Logic: Only rotate if the new signal is strong enough!
            # Rotation threshold: daily >= 2.5 OR hourly >= 2.0
            is_rotation_execution = False
            if buying_power <= 0 and can_rotate:
                if change_pct >= 2.5 or (hourly_change_pct is not None and hourly_change_pct >= 2.0):
                    is_rotation_execution = True
                else:
                    report["action_taken"] = "SKIP"
                    report["details"] = f"Stale position {stale_symbol} flagged, but best new signal {symbol} is not strong enough to trigger rotation (needs daily >= 2.5% or hourly >= 2.0%)."
                    finalize()
                    return

            # Sizing and Signal Strength: evaluate daily and hourly tiers
            # independently, then keep whichever yields the larger size.
            max_position_pct = float(os.getenv("MAX_POSITION_PCT", "0.50"))

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

            order_size_usd = equity * max_position_pct * sizing_mult
        
            # If executing rotation, we sell the stale position FIRST!
            if is_rotation_execution:
                close_url = f"{ALPACA_BASE_URL}/v2/positions/{stale_symbol}"
                close_res = requests.delete(close_url, headers=headers, timeout=10)
                if close_res.status_code in [200, 201, 204]:
                    should_notify = True
                    msg_lines.append(f"🔄 **Περιστροφή Crypto**: Πωλήθηκε το στάσιμο **{stale_symbol}** (+{round(stale_unrealized_plpc, 2)}% μετά από {round(stale_age_hours, 2)} ώρες).")
                
                    log_trade(db_conn,
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
                        save_trading_state(db_conn, EXCHANGE_NAME, new_state)
                    positions = [p for p in positions if p["symbol"] != stale_symbol]
                    
                    # Wait 1.5 seconds for Alpaca to update buying power
                    time.sleep(1.5)
                
                    # Refresh account info for updated buying power
                    acc_res = requests.get(acc_url, headers=headers, timeout=10)
                    if acc_res.status_code == 200:
                        account = acc_res.json()
                        buying_power = float(account.get("buying_power", 0.0))
                else:
                    report["action_taken"] = "ROTATE_FAILED"
                    report["details"] = f"Failed to close stale position {stale_symbol} to initiate rotation."
                    finalize()
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
                order_res = requests.post(order_url, headers=headers, json=order_data, timeout=10)
            
                if order_res.status_code in [200, 201]:
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
                
                    # Human-readable momentum tag, noting the hourly driver when it wins.
                    if hourly_is_primary:
                        momentum_desc = f"{signal_strength} (+{round(hourly_change_pct, 2)}% 1h)"
                    else:
                        momentum_desc = f"{signal_strength} (+{round(change_pct, 2)}% intraday)"

                    if is_rotation_execution:
                        msg_lines.append(f"🛒 **Περιστροφή Crypto**: Αγοράστηκε το νέο hot **{symbol}** (${round(order_size_usd, 2)} - {momentum_desc}!)")
                    else:
                        msg_lines.append(f"🛒 **Αγοράστηκε {symbol}** (${round(order_size_usd, 2)} - {momentum_desc})")

                    log_trade(db_conn,
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
                        reason=f"{momentum_desc} on {symbol}. Deployed ${round(order_size_usd, 2)} per {reason_rule}."
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

    # --- HOURLY NOTIFICATION HEARTBEAT + NOTIFY ---
    finalize()

if __name__ == "__main__":
    run_cycle()
