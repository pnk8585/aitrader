import os
import sys
import json
import ccxt
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv

def get_last_n_lines(filepath, n=15):
    if not os.path.exists(filepath):
        return []
    try:
        with open(filepath, "r") as f:
            lines = f.readlines()
            return [line.strip() for line in lines[-n:]]
    except Exception as e:
        return [f"Error reading log {filepath}: {e}"]

def main():
    env_path = "PROJECT_ROOT/.env"
    load_dotenv(dotenv_path=env_path)

    diagnostics = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "alpaca": {},
        "kraken": {},
        "recent_logs": {}
    }

    # 1. Fetch Alpaca Diagnostics
    alpaca_key = os.getenv("ALPACA_API_KEY")
    alpaca_secret = os.getenv("ALPACA_SECRET_KEY")
    alpaca_base = "https://api.alpaca.markets"
    
    if alpaca_key and alpaca_secret:
        headers = {
            "APCA-API-KEY-ID": alpaca_key,
            "APCA-API-SECRET-KEY": alpaca_secret,
            "Content-Type": "application/json"
        }
        try:
            acc_res = requests.get(f"{alpaca_base}/v2/account", headers=headers, timeout=10)
            pos_res = requests.get(f"{alpaca_base}/v2/positions", headers=headers, timeout=10)
            
            if acc_res.status_code == 200:
                acc = acc_res.json()
                diagnostics["alpaca"]["account"] = {
                    "equity": acc.get("equity"),
                    "cash": acc.get("cash"),
                    "buying_power": acc.get("buying_power"),
                    "pattern_day_trader": acc.get("pattern_day_trader")
                }
            else:
                diagnostics["alpaca"]["account_error"] = f"Status {acc_res.status_code}: {acc_res.text}"

            if pos_res.status_code == 200:
                diagnostics["alpaca"]["positions"] = pos_res.json()
            else:
                diagnostics["alpaca"]["positions_error"] = f"Status {pos_res.status_code}: {pos_res.text}"
        except Exception as e:
            diagnostics["alpaca"]["error"] = str(e)
    else:
        diagnostics["alpaca"]["error"] = "Keys missing in .env"

    # 2. Fetch Kraken Diagnostics
    kraken_key = os.getenv("KRAKEN_API_KEY")
    kraken_secret = os.getenv("KRAKEN_SECRET")
    
    if kraken_key and kraken_secret:
        try:
            exchange = ccxt.kraken({
                'apiKey': kraken_key,
                'secret': kraken_secret,
                'enableRateLimit': True
            })
            balance = exchange.fetch_balance()
            
            # Fetch prices for active assets to calculate total portfolio value
            symbols = ['BTC/EUR', 'ETH/EUR', 'SOL/EUR', 'AVAX/EUR', 'LINK/EUR', 'XRP/EUR', 'DOGE/EUR', 'SUI/EUR', 'NEAR/EUR', 'RENDER/EUR', 'ADA/EUR', 'DOT/EUR']
            tickers = exchange.fetch_tickers(symbols)
            
            cash_eur = balance['total'].get('EUR', 0.0)
            total_equity = cash_eur
            active_positions = []
            
            for sym in symbols:
                coin = sym.split('/')[0]
                qty = balance['total'].get(coin, 0.0)
                ticker = tickers.get(sym)
                if not ticker:
                    continue
                price = ticker['last']
                val_eur = qty * price
                if val_eur > 1.0: # dust limit 1 EUR
                    total_equity += val_eur
                    active_positions.append({
                        "symbol": sym,
                        "qty": qty,
                        "current_price": price,
                        "value_eur": val_eur
                    })
            
            diagnostics["kraken"]["account"] = {
                "equity_eur": total_equity,
                "cash_eur": cash_eur,
            }
            diagnostics["kraken"]["positions"] = active_positions
        except Exception as e:
            diagnostics["kraken"]["error"] = str(e)
    else:
        diagnostics["kraken"]["error"] = "Keys missing in .env"

    # 3. Read Trade Logs
    log_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    alpaca_log_path = f"PROJECT_ROOT/logs/trades-{log_date}.jsonl"
    kraken_log_path = f"PROJECT_ROOT/logs/kraken_trades-{log_date}.jsonl"
    
    diagnostics["recent_logs"]["alpaca_trades"] = get_last_n_lines(alpaca_log_path, 10)
    diagnostics["recent_logs"]["kraken_trades"] = get_last_n_lines(kraken_log_path, 10)

    # 4. Check for state files
    diagnostics["state_files"] = {
        "alpaca_state_exists": os.path.exists("PROJECT_ROOT/traders/extreme/state.json"),
        "kraken_state_exists": os.path.exists("PROJECT_ROOT/traders/extreme/kraken_state.json")
    }

    print(json.dumps(diagnostics, indent=2))

if __name__ == "__main__":
    main()
