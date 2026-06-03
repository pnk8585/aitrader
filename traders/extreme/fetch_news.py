import os
import sys
import requests
from dotenv import load_dotenv

def main():
    ROOT_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))
    env_path = os.path.join(ROOT_DIR, ".env")
    load_dotenv(dotenv_path=env_path)

    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")

    if not api_key or not secret_key:
        print("Error: Missing Alpaca API keys in .env", file=sys.stderr)
        sys.exit(1)

    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": secret_key,
        "Content-Type": "application/json"
    }

    # Fetch latest 10 news items for our crypto pairs
    symbols = "BTCUSD,ETHUSD,SOLUSD,AVAXUSD,LINKUSD"
    url = f"https://data.alpaca.markets/v1beta1/news?symbols={symbols}&limit=10"

    try:
        res = requests.get(url, headers=headers, timeout=15)
        if res.status_code != 200:
            print(f"Error: Alpaca API returned status code {res.status_code}", file=sys.stderr)
            print(res.text, file=sys.stderr)
            sys.exit(1)
        
        data = res.json()
        news_items = data.get("news", [])
        
        if not news_items:
            print("No recent crypto news found.")
            return

        print("--- LATEST CRYPTO NEWS ---")
        for idx, item in enumerate(news_items, 1):
            headline = item.get("headline", "N/A")
            summary = item.get("summary", "N/A")
            created_at = item.get("created_at", "N/A")
            url = item.get("url", "N/A")
            related_symbols = item.get("symbols", [])
            
            print(f"\n[{idx}] {headline}")
            print(f"Timestamp: {created_at}")
            print(f"Tickers: {', '.join(related_symbols)}")
            if summary and summary != headline:
                print(f"Summary: {summary}")
            print(f"URL: {url}")
            print("-" * 40)

    except Exception as e:
        print(f"Exception while fetching news: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
