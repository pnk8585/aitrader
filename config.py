# Configuration for Trading Bot

# Stocks to monitor
WATCHLIST = ["AAPL", "NVDA", "TSLA"]

# Sentiment thresholds
BUY_THRESHOLD = 6
SELL_THRESHOLD = -6

# Trade settings
TRADE_AMOUNT = 1000 # Dollars per trade

# Proxy settings
PROXY_URL = "http://172.16.0.50:4000/v1/chat/completions"
MODEL_NAME = "gemini-1.5-flash"
