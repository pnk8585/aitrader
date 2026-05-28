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

# Universe configurations
MOMENTUM_STOCKS = [
    "TSLA", "NVDA", "AMD", "MSTR", "COIN", "SMCI", 
    "PLTR", "ROKU", "SNAP", "SHOP", "AAPL", "MSFT",
    "GOOGL", "AMZN", "META", "NFLX", "CRM", "UBER",
    "ABNB", "PYPL", "SQ", "HOOD", "SOFI", "LCID",
    "RIVN", "NIO", "XPEV", "LI", "QS", "SPCE"
]
CRYPTO_PAIRS = ["BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD", "LINK/USD"]

LOG_DIR = "PROJECT_ROOT/logs"
os.makedirs(LOG_DIR, exist_ok=True)

STATE_FILE = "PROJECT_ROOT/traders/extreme/state.json"
PREMARKET_TARGET_FILE = "PROJECT_ROOT/traders/extreme/premarket_target.json"

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
        print(f"Error saving state: {e}")

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
        print(f"Error checking position age for {symbol}: {e}")
    return 0.0

def fetch_premarket_news():
    news_items = []
    batch1 = MOMENTUM_STOCKS[:15]
    batch2 = MOMENTUM_STOCKS[15:]
    
    for batch in [batch1, batch2]:
        symbols_str = ",".join(batch)
        news_url = f"https://data.alpaca.markets/v1beta1/news?symbols={symbols_str}&limit=15"
        try:
            res = requests.get(news_url, headers=headers)
            if res.status_code == 200:
                data = res.json().get("news", [])
                for item in data:
                    news_items.append({
                        "symbols": item.get("symbols", []),
                        "headline": item.get("headline", ""),
                        "summary": item.get("summary", ""),
                        "created_at": item.get("created_at", ""),
                        "url": item.get("url", "")
                    })
        except Exception as e:
            print(f"Error fetching news: {e}")
            
    news_items = sorted(news_items, key=lambda x: x["created_at"], reverse=True)
    return news_items[:15] # Keep top 15 news items

def run_cycle():
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "success",
        "positions_managed": [],
        "scanned_assets": [],
        "action_taken": "NONE",
        "details": ""
    }
    
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
    
    # 2. Fetch Clock Info
    clock_url = f"{ALPACA_BASE_URL}/v2/clock"
    clock_res = requests.get(clock_url, headers=headers)
    market_open = False
    clock_data = {}
    if clock_res.status_code == 200:
        clock_data = clock_res.json()
        market_open = clock_data.get("is_open", False)
    
    # 3. Fetch Positions
    pos_url = f"{ALPACA_BASE_URL}/v2/positions"
    pos_res = requests.get(pos_url, headers=headers)
    positions = []
    if pos_res.status_code == 200:
        positions = pos_res.json()
        
    report["portfolio_equity"] = equity
    report["buying_power"] = buying_power
    report["market_open"] = market_open
    report["open_positions_count"] = len(positions)
    
    # Check for End of Day Liquidation window (10 minutes before close)
    liquidation_window = False
    if market_open and clock_data.get("next_close"):
        try:
            next_close = datetime.fromisoformat(clock_data["next_close"])
            now_utc = datetime.now(timezone.utc)
            time_to_close_seconds = (next_close - now_utc).total_seconds()
            if 0 < time_to_close_seconds <= 600: # 10 minutes = 600s
                liquidation_window = True
        except Exception as e:
            print(f"Error calculating liquidation window: {e}")
            
    # Check for Pre-market scanning window (30 minutes before open)
    premarket_window = False
    if not market_open and clock_data.get("next_open"):
        try:
            next_open = datetime.fromisoformat(clock_data["next_open"])
            now_utc = datetime.now(timezone.utc)
            time_to_open_seconds = (next_open - now_utc).total_seconds()
            if 0 < time_to_open_seconds <= 1800: # 30 minutes = 1800s
                premarket_window = True
        except Exception as e:
            print(f"Error calculating pre-market window: {e}")

    report["liquidation_active"] = liquidation_window
    report["premarket_active"] = premarket_window

    # --- END OF DAY LIQUIDATION ---
    if liquidation_window:
        report["action_taken"] = "LIQUIDATION"
        liquidated_symbols = []
        for pos in positions:
            symbol = pos["symbol"]
            qty = float(pos["qty"])
            entry_price = float(pos["avg_entry_price"])
            current_price = float(pos["current_price"])
            unrealized_plpc = float(pos["unrealized_plpc"])
            
            close_url = f"{ALPACA_BASE_URL}/v2/positions/{symbol}"
            close_res = requests.delete(close_url, headers=headers)
            if close_res.status_code in [200, 201, 204]:
                liquidated_symbols.append(symbol)
                log_trade(
                    action="SELL",
                    ticker=symbol,
                    asset_type="STOCK",
                    signal_strength="EOD_LIQUIDATION",
                    momentum_pct=0.0,
                    entry_price=entry_price,
                    current_price=current_price,
                    unrealized_plpc=unrealized_plpc,
                    order_id=close_res.json().get("id") if close_res.text else None,
                    client_order_id=None,
                    quantity=qty,
                    estimated_value_usd=qty * current_price,
                    position_size_pct=0.0,
                    portfolio_equity=equity,
                    reason="End of Day Liquidation - Starting clean tomorrow!"
                )
        # Clear peaks state
        save_state({})
        report["details"] = f"End of Day liquidation active. Closed all positions: {', '.join(liquidated_symbols) if liquidated_symbols else 'None'}."
        print(json.dumps(report))
        return

    # --- PRE-MARKET CATALYST RESEARCH ---
    if premarket_window:
        report["action_taken"] = "PRE_MARKET_SCAN"
        news = fetch_premarket_news()
        report["premarket_news"] = news
        report["details"] = f"Pre-market window active. Fetched {len(news)} latest news articles."
        print(json.dumps(report))
        return

    # Load and prepare state
    state = load_state()
    new_state = {}
    active_symbols = [p["symbol"] for p in positions]
    for sym in active_symbols:
        new_state[sym] = state.get(sym, {"peak_plpc": 0.0})
    
    # 4. Manage Open Positions First (during regular market hours)
    managed_any = False
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
        
        # Trailing Take Profit (TTP)
        if peak_plpc >= 2.0 and unrealized_plpc <= (peak_plpc - 0.75):
            sell_triggered = True
            sell_reason = f"Trailing Take Profit hit (Peak: +{round(peak_plpc, 2)}% | Sold at: +{round(unrealized_plpc, 2)}%)"
        # Profit Lock
        elif peak_plpc >= 2.5 and unrealized_plpc < 2.0:
            sell_triggered = True
            sell_reason = f"Profit lock protection (Peak: +{round(peak_plpc, 2)}% | Sold at: +{round(unrealized_plpc, 2)}%)"
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
            "reason": f"Within limits (peak: +{round(peak_plpc, 2)}%)"
        }
        
        if sell_triggered:
            if asset_type == "CRYPTO" or market_open:
                close_url = f"{ALPACA_BASE_URL}/v2/positions/{symbol}"
                close_res = requests.delete(close_url, headers=headers)
                if close_res.status_code in [200, 201, 204]:
                    pos_report["action"] = "SELL"
                    pos_report["reason"] = sell_reason
                    managed_any = True
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
            
    # 5. Check if we have open positions limit
    if len(positions) >= 5:
        report["action_taken"] = "SKIP"
        report["details"] = "Max positions limit reached (5 open positions)."
        print(json.dumps(report))
        return
        
    if buying_power <= 0:
        report["action_taken"] = "SKIP"
        report["details"] = "No buying power available."
        # Log SKIP
        for pos in positions:
            symbol = pos["symbol"]
            unrealized_plpc = float(pos["unrealized_plpc"])
            qty = float(pos["qty"])
            entry_price = float(pos["avg_entry_price"])
            current_price = float(pos["current_price"])
            asset_type = "STOCK" if pos.get("asset_class") == "us_equity" else "CRYPTO"
            log_trade(
                action="SKIP",
                ticker=symbol,
                asset_type=asset_type,
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

    # --- EXECUTE PRE-MARKET TARGET IF EXISTS ---
    if os.path.exists(PREMARKET_TARGET_FILE) and market_open:
        try:
            with open(PREMARKET_TARGET_FILE, "r") as f:
                target_data = json.load(f)
            symbol = target_data.get("symbol")
            reason = target_data.get("reason", "Pre-market catalyst")
            
            if symbol and not any(p["symbol"] == symbol for p in positions):
                # Order size
                order_size_usd = buying_power
                if equity >= 200.0:
                    max_position_pct = float(os.getenv("MAX_POSITION_PCT", "0.50"))
                    order_size_usd = equity * max_position_pct
                if order_size_usd > buying_power:
                    order_size_usd = buying_power
                
                if order_size_usd >= 1.0:
                    timestamp_suffix = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                    client_order_id = f"extreme-{symbol.replace('/', '')}-pre-{timestamp_suffix}"
                    
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
                    
                    # Delete target file so we only buy once
                    try:
                        os.remove(PREMARKET_TARGET_FILE)
                    except:
                        pass
                        
                    if order_res.status_code in [200, 201]:
                        order = order_res.json()
                        report["action_taken"] = "BUY"
                        report["details"] = f"Successfully executed pre-market target {symbol}: {reason}"
                        report["order_id"] = order.get("id")
                        
                        log_trade(
                            action="BUY",
                            ticker=symbol,
                            asset_type="STOCK",
                            signal_strength="PREMARKET_CATALYST",
                            momentum_pct=0.0,
                            entry_price=0.0,
                            current_price=0.0,
                            unrealized_plpc=0.0,
                            order_id=order.get("id"),
                            client_order_id=client_order_id,
                            quantity=0.0,
                            estimated_value_usd=order_size_usd,
                            position_size_pct=order_size_usd / equity * 100.0,
                            portfolio_equity=equity,
                            reason=f"PREMARKET_CATALYST Buy: {reason}. Deployed ${round(order_size_usd, 2)}."
                        )
                        print(json.dumps(report))
                        return
        except Exception as e:
            print(f"Error executing pre-market target: {e}")
            try:
                os.remove(PREMARKET_TARGET_FILE)
            except:
                pass

    # --- REGULAR HOUR MOMENTUM SCAN ---
    candidates = []
    asset_type = "STOCK"
    
    if market_open:
        batch_size = 10
        for i in range(0, len(MOMENTUM_STOCKS), batch_size):
            batch = MOMENTUM_STOCKS[i:i+batch_size]
            symbols_str = ",".join(batch)
            snap_url = f"https://data.alpaca.markets/v2/stocks/snapshots?symbols={symbols_str}"
            snap_res = requests.get(snap_url, headers=headers)
            if snap_res.status_code == 200:
                data = snap_res.json()
                for symbol, snap in data.items():
                    if not snap:
                        continue
                    if any(p["symbol"] == symbol for p in positions):
                        continue
                    
                    prev_daily = snap.get("prevDailyBar")
                    if not prev_daily or prev_daily.get("v", 0) < 1000000:
                        continue
                        
                    daily = snap.get("dailyBar")
                    if not daily:
                        continue
                    open_price = float(daily.get("o", 0.0))
                    if open_price <= 0:
                        continue
                    
                    current_price = 0.0
                    min_bar = snap.get("minuteBar")
                    if min_bar:
                        current_price = float(min_bar.get("c", 0.0))
                    if current_price <= 0:
                        trade = snap.get("latestTrade")
                        if trade:
                            current_price = float(trade.get("p", 0.0))
                            
                    if current_price <= 0:
                        continue
                        
                    change_pct = (current_price - open_price) / open_price * 100.0
                    candidates.append({
                        "symbol": symbol,
                        "change_pct": change_pct,
                        "current_price": current_price,
                        "volume": prev_daily.get("v", 0)
                    })
    
    # Fallback to crypto
    stock_signals = [c for c in candidates if c["change_pct"] >= 2.0]
    if not market_open or not stock_signals:
        asset_type = "CRYPTO"
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
    
    if not signals:
        report["action_taken"] = "SKIP"
        report["details"] = f"No momentum signals found above +2% threshold. Scanned {len(candidates)} symbols."
        print(json.dumps(report))
        return
        
    best_candidate = signals[0]
    symbol = best_candidate["symbol"]
    change_pct = best_candidate["change_pct"]
    current_price = best_candidate["current_price"]
    
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
    
    if equity < 200.0:
        order_size_usd = buying_power
        reason_rule = "small account rule (< $200 equity)"
    else:
        reason_rule = f"{signal_strength} level sizing"
        
    if order_size_usd > buying_power:
        order_size_usd = buying_power
        
    if order_size_usd < 1.0:
        report["action_taken"] = "SKIP"
        report["details"] = f"Calculated order size of ${round(order_size_usd, 2)} is too small."
        print(json.dumps(report))
        return
        
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
        
        log_trade(
            action="BUY",
            ticker=symbol,
            asset_type=asset_type,
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
        
    print(json.dumps(report))

if __name__ == "__main__":
    run_cycle()
