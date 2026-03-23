# Trading Bot Configuration

## Watchlist
AAPL, NVDA, TSLA, SPY, BTC/USD, ETH/USD

## Thresholds
- BUY if sentiment score >= 6 (scale: -10 to +10)
- SELL if sentiment score <= -6
- HOLD otherwise

## Trade Amount
$500 notional per trade

## Max Position
Do not buy more of a ticker if current position value already exceeds $2000

## News Sources
- Yahoo Finance RSS: per ticker (no API key needed)
- Google News RSS: per ticker (no API key needed)

## Trading Mode
Paper trading only — base URL: https://paper-api.alpaca.markets
