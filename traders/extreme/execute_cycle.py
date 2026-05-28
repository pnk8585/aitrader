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

# Universe configurations (Added Leveraged ETFs: TQQQ, SOXL)
MOMENTUM_STOCKS = [
    "TQQQ", "SOXL", "TSLA", "NVDA", "AMD", "MSTR", "COIN", "SMCI", 
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
            print(f"Error fetching news: {e}", file=sys.stderr)
            
    news_items = sorted(news_items, key=lambda x: x["created_at"], reverse=True)
    return news_items[:15]

def get_premarket_target_from_llm(news_items):
    if not news_items:
        return None
    
    news_text = ""
    for item in news_items:
        symbols = ", ".join(item.get("symbols", []))
        news_text += f"Ticker(s): {symbols}\nHeadline: {item.get('headline')}\nSummary: {item.get('summary')}\n---\n"
        
    prompt = f"""You are a senior financial analyst. Read these pre-market news headlines for our watchlist of momentum stocks.
Identify if there is any major, highly positive company-specific catalyst (such as blowout earnings beat, major merger/deal, FDA approval, or analyst upgrade).
If you find one, select the single most promising stock.

Watchlist: {MOMENTUM_STOCKS}

News headlines:
{news_text}

You must respond in strict JSON format:
{{"symbol": "TICKER", "reason": "A very brief 1-sentence reason detailing the catalyst"}}
If no stock has a major positive catalyst today, respond with:
{{"symbol": null, "reason": "No major catalyst found"}}
"""

    headers_llm = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {os.getenv('LITELLM_API_KEY', '')}"
    }
    
    data_llm = {
        "model": "gemini-1.5-flash",
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"}
    }
    
    try:
        res = requests.post("http://172.16.0.50:4000/v1/chat/completions", headers=headers_llm, json=data_llm, timeout=30)
        if res.status_code == 200:
            content = res.json()["choices"][0]["message"]["content"]
            result = json.loads(content)
            return result
    except Exception as e:
        print(f"Error calling local LLM proxy: {e}", file=sys.stderr)
    return None

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
    
    # Market Open / Close state transition check
    last_market_open = notify_state.get("last_market_open", False)
    if market_open != last_market_open:
        should_notify = True
        if market_open:
            msg_lines.append("🔔 **Η αγορά άνοιξε!**")
        else:
            msg_lines.append("🔕 **Η αγορά έκλεισε!**")
            
    # Check for End of Day Liquidation window (10 minutes before close)
    liquidation_window = False
    if market_open and clock_data.get("next_close"):
        try:
            next_close = datetime.fromisoformat(clock_data["next_close"])
            now_utc = datetime.now(timezone.utc)
            time_to_close_seconds = (next_close - now_utc).total_seconds()
            if 0 < time_to_close_seconds <= 600: 
                liquidation_window = True
        except Exception as e:
            print(f"Error calculating liquidation window: {e}", file=sys.stderr)
            
    # Check for Sunday Night Liquidation (10 minutes before Monday open)
    now_local = datetime.now()
    is_sunday_night = (now_local.weekday() == 6 and now_local.hour == 22 and now_local.minute >= 50)
    if is_sunday_night:
        liquidation_window = True
            
    # Check for Pre-market scanning window (30 minutes before open)
    premarket_window = False
    if not market_open and clock_data.get("next_open"):
        try:
            next_open = datetime.fromisoformat(clock_data["next_open"])
            now_utc = datetime.now(timezone.utc)
            time_to_open_seconds = (next_open - now_utc).total_seconds()
            if 0 < time_to_open_seconds <= 1800: 
                premarket_window = True
        except Exception as e:
            print(f"Error calculating pre-market window: {e}", file=sys.stderr)

    report["liquidation_active"] = liquidation_window
    report["premarket_active"] = premarket_window

    # --- END OF DAY / WEEKEND LIQUIDATION ---
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
                should_notify = True
                log_trade(
                    action="SELL",
                    ticker=symbol,
                    asset_type="STOCK" if symbol not in CRYPTO_PAIRS else "CRYPTO",
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
                    reason="End of Day Liquidation - Starting clean tomorrow!" if not is_sunday_night else "Weekend Crypto Liquidation - Starting clean for stock market Monday!"
                )
                
        # Clear peaks state
        save_state({})
        
        if liquidated_symbols:
            msg_lines.append(f"🧹 **EOD Liquidation**: Πωλήθηκαν όλα: {', '.join(liquidated_symbols)}")
        else:
            msg_lines.append("🧹 **EOD Liquidation**: Το χαρτοφυλάκιο είναι ήδη καθαρό.")
            
        report["details"] = f"Liquidation active. Closed all positions: {', '.join(liquidated_symbols) if liquidated_symbols else 'None'}."
        
        # Output notification
        print("\n".join(msg_lines))
        notify_state["last_notify_time"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        notify_state["last_market_open"] = market_open
        save_notify_state(notify_state)
        return

    # --- PRE-MARKET CATALYST RESEARCH & LIQUIDATION ---
    if premarket_window:
        report["action_taken"] = "PRE_MARKET_SCAN"
        
        # Automatic Pre-Market Liquidation of any overnight positions
        liquidated_symbols = []
        if positions:
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
                    should_notify = True
                    log_trade(
                        action="SELL",
                        ticker=symbol,
                        asset_type="STOCK" if symbol not in CRYPTO_PAIRS else "CRYPTO",
                        signal_strength="PREMARKET_LIQUIDATION",
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
                        reason="Pre-market Liquidation - Closing overnight positions to start stock day clean!"
                    )
            # Clear peaks state
            save_state({})
            
        if liquidated_symbols:
            msg_lines.append(f"🧹 **Pre-market Clean-up**: Κλείσιμο overnight θέσεων: {', '.join(liquidated_symbols)}")
            
        # Fetch premarket news
        news = fetch_premarket_news()
        
        # Call local LLM to find if there is an explosive catalyst
        target_res = get_premarket_target_from_llm(news)
        if target_res and target_res.get("symbol"):
            sym = target_res["symbol"]
            reason_en = target_res["reason"]
            
            # Save target
            with open(PREMARKET_TARGET_FILE, "w") as f:
                json.dump({"symbol": sym, "reason": reason_en}, f)
                
            should_notify = True
            msg_lines.append(f"🎯 **Premarket Catalyst**: Εντοπίστηκε καταλύτης για τη **{sym}**!")
            msg_lines.append(f"📝 {reason_en}")
            msg_lines.append("🛒 Θα γίνει αυτόματη αγορά στο άνοιγμα.")
            
        report["details"] = f"Pre-market window active. Liquidated overnight: {liquidated_symbols}. Target chosen: {target_res}."
        
        if should_notify:
            print("\n".join(msg_lines))
            notify_state["last_notify_time"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        notify_state["last_market_open"] = market_open
        save_notify_state(notify_state)
        return

    # Load peaks state
    new_state = {}
    active_symbols = [p["symbol"] for p in positions]
    for sym in active_symbols:
        new_state[sym] = state.get(sym, {"peak_plpc": 0.0})
    
    # 4. Manage Open Positions First
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
    
    # 6. Execute premarket target if available
    elif os.path.exists(PREMARKET_TARGET_FILE) and market_open:
        try:
            with open(PREMARKET_TARGET_FILE, "r") as f:
                target_data = json.load(f)
            symbol = target_data.get("symbol")
            reason = target_data.get("reason", "Pre-market catalyst")
            
            if symbol and not any(p["symbol"] == symbol for p in positions):
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
                    
                    try:
                        os.remove(PREMARKET_TARGET_FILE)
                    except:
                        pass
                        
                    if order_res.status_code in [200, 201]:
                        order = order_res.json()
                        report["action_taken"] = "BUY"
                        report["details"] = f"Successfully executed pre-market target {symbol}: {reason}"
                        report["order_id"] = order.get("id")
                        should_notify = True
                        msg_lines.append(f"🛒 **Αγοράστηκε {symbol}** (${round(order_size_usd, 2)} - Premarket Catalyst)")
                        
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
        except Exception as e:
            print(f"Error executing pre-market target: {e}", file=sys.stderr)
            try:
                os.remove(PREMARKET_TARGET_FILE)
            except:
                pass

    # 7. Regular Hour Momentum Scan (if we have buying power and no buy executed yet)
    elif buying_power > 0:
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
        
        if signals:
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
                    msg_lines.append(f"🛒 **Αγοράστηκε {symbol}** (${round(order_size_usd, 2)} - {signal_strength})")
                    
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
        # Append current position info
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
            
        # Print to stdout so Hermes delivers it verbatim
        print("\n".join(msg_lines))
        
        # Save notify state with updated time
        notify_state["last_notify_time"] = now_utc.isoformat().replace("+00:00", "Z")
        
    notify_state["last_market_open"] = market_open
    save_notify_state(notify_state)

if __name__ == "__main__":
    run_cycle()
