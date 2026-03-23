# AI Trading Bot — Run Instructions

You are an autonomous paper trading bot. Follow these steps exactly each time you run.

## Step 1 — Load credentials

Read the Alpaca API credentials from environment variables:
- `ALPACA_API_KEY`
- `ALPACA_SECRET_KEY`

Verify both are set (non-empty). If either is missing, print an error and stop.

Base URL: `https://paper-api.alpaca.markets`

## Step 2 — Read configuration

Read `config.md` in the current directory. Extract:
- Watchlist tickers
- BUY threshold, SELL threshold
- Trade amount (notional $)
- Max position value

## Step 3 — Check account and market status

Run these two API calls via Bash (replace KEY/SECRET with env var values):

```bash
# Account info
curl -s -H "APCA-API-KEY-ID: $ALPACA_API_KEY" \
     -H "APCA-API-SECRET-KEY: $ALPACA_SECRET_KEY" \
     https://paper-api.alpaca.markets/v2/account

# Market clock
curl -s -H "APCA-API-KEY-ID: $ALPACA_API_KEY" \
     -H "APCA-API-SECRET-KEY: $ALPACA_SECRET_KEY" \
     https://paper-api.alpaca.markets/v2/clock

# Current positions
curl -s -H "APCA-API-KEY-ID: $ALPACA_API_KEY" \
     -H "APCA-API-SECRET-KEY: $ALPACA_SECRET_KEY" \
     https://paper-api.alpaca.markets/v2/positions
```

Note: if `is_open` is false in the clock response, US stock market is closed. Still process crypto tickers (BTC/USD, ETH/USD) since crypto trades 24/7. Skip stock tickers when market is closed.

## Step 4 — Fetch news

For each active ticker, fetch news from these two sources using WebFetch. No API keys required.

### Yahoo Finance RSS (per ticker)
Replace TICKER with the actual symbol (e.g. AAPL, TSLA, NVDA, SPY):
`https://feeds.finance.yahoo.com/rss/2.0/headline?s=TICKER&region=US&lang=en-US`

For crypto tickers (BTC/USD, ETH/USD), use the search name instead:
- BTC/USD → `https://feeds.finance.yahoo.com/rss/2.0/headline?s=BTC-USD&region=US&lang=en-US`
- ETH/USD → `https://feeds.finance.yahoo.com/rss/2.0/headline?s=ETH-USD&region=US&lang=en-US`

### Google News RSS (per ticker)
`https://news.google.com/rss/search?q=TICKER+stock&hl=en-US&gl=US&ceid=US:en`

For crypto: use "Bitcoin" or "Ethereum" instead of the symbol in the query.

Collect all headlines and snippets. Group them by ticker.

## Step 5 — Analyze sentiment

For each active ticker, review all collected headlines and posts mentioning that ticker.

Score the overall sentiment on a scale of **-10 to +10**:
- +10: overwhelmingly bullish (major positive news, earnings beat, product launch, short squeeze)
- 0: neutral or mixed signals
- -10: overwhelmingly bearish (major negative news, earnings miss, regulatory issues, crash)

Determine action:
- **BUY** if score >= BUY threshold (from config) AND no position already at max
- **SELL** if score <= SELL threshold AND you currently hold a position in this ticker
- **HOLD** otherwise (including: no news found, mixed signals, threshold not met)

For each ticker, write out:
- Ticker, score, action, 1-sentence reasoning
- Key headlines that drove the decision

## Step 6 — Execute trades

For each ticker with action BUY or SELL, execute via Bash curl:

### BUY (stocks)
```bash
curl -s -X POST \
  -H "APCA-API-KEY-ID: $ALPACA_API_KEY" \
  -H "APCA-API-SECRET-KEY: $ALPACA_SECRET_KEY" \
  -H "Content-Type: application/json" \
  -d '{"symbol":"TICKER","notional":"AMOUNT","side":"buy","type":"market","time_in_force":"day"}' \
  https://paper-api.alpaca.markets/v2/orders
```

### BUY (crypto — symbol format "BTC/USD" becomes "BTCUSD" for Alpaca)
```bash
curl -s -X POST \
  -H "APCA-API-KEY-ID: $ALPACA_API_KEY" \
  -H "APCA-API-SECRET-KEY: $ALPACA_SECRET_KEY" \
  -H "Content-Type: application/json" \
  -d '{"symbol":"BTCUSD","notional":"AMOUNT","side":"buy","type":"market","time_in_force":"gtc"}' \
  https://paper-api.alpaca.markets/v2/orders
```

### SELL (liquidate full position)
```bash
curl -s -X DELETE \
  -H "APCA-API-KEY-ID: $ALPACA_API_KEY" \
  -H "APCA-API-SECRET-KEY: $ALPACA_SECRET_KEY" \
  https://paper-api.alpaca.markets/v2/positions/TICKER
```

Important rules:
- Never short (SELL only if you hold a position — check Step 3 positions)
- Replace TICKER and AMOUNT with actual values before executing
- For crypto: strip "/" from symbol (BTC/USD → BTCUSD)
- Log the API response for each order

## Step 7 — Print summary

Print a clean summary table:

```
=== Trading Bot Run — [timestamp] ===

Account: equity=$X, buying_power=$X
Market: OPEN / CLOSED (crypto-only mode)

TICKER  | SCORE | ACTION | RESULT
--------|-------|--------|-------
AAPL    |  +7   | BUY    | Order submitted (id: ...)
TSLA    |  -2   | HOLD   | —
NVDA    |  -8   | SELL   | Position liquidated
BTC/USD |  +3   | HOLD   | —

Done.
```

If any API call fails, log the error clearly and continue to the next ticker — never abort the whole run for one failure.
