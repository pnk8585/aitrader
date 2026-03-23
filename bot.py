import os
import time
import alpaca_trade_api as tradeapi
from dotenv import load_dotenv
from analyzer import analyze_sentiment
from config import BUY_THRESHOLD, SELL_THRESHOLD

load_dotenv()

# Initialize Alpaca
api = tradeapi.REST(
    os.getenv("ALPACA_API_KEY"),
    os.getenv("ALPACA_SECRET_KEY"),
    "https://paper-api.alpaca.markets",
    api_version='v2'
)

def main():
    print("Bot started...")
    # This is a placeholder for the news/tweet stream
    # In a real bot, we'll use a websocket or polling service
    # For now, we'll just test with a mock headline
    headline = "Trump announces new tariffs on imports"
    asset = "SPY"
    
    score = analyze_sentiment(headline, asset)
    print(f"Headline: {headline} | Sentiment Score: {score}")
    
    if score <= SELL_THRESHOLD:
        print(f"Bearish sentiment detected ({score}). Placing sell order for {asset}...")
        # api.submit_order(...)
    elif score >= BUY_THRESHOLD:
        print(f"Bullish sentiment detected ({score}). Placing buy order for {asset}...")
        # api.submit_order(...)

if __name__ == "__main__":
    main()
