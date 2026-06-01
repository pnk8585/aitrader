# Code Review: New Strategy Files

Review the 3 new strategy files created during restructuring. Focus on correctness, edge cases, bugs, and potential runtime failures.

## Files to Review

### 1. PROJECT_ROOT/traders/crypto_trades/kraken_pullback.py (777 lines)
Port of the original kraken v2 pullback strategy. Key changes from original:
- EXCHANGE_NAME = "kraken-pullback" (was "kraken")
- PRICE_EXCHANGE = "kraken" (added for price-read queries, since insert_prices hardcodes 'kraken')
- LOCK_FILE = logs/kraken_pullback.lock
- Import uses sys.path -> traders/extreme/db_prices

### 2. PROJECT_ROOT/traders/crypto_trades/kraken_momentum.py (764 lines)
New momentum breakout strategy using CCXT/Kraken:
- EXCHANGE_NAME = "kraken-momentum"
- Entry: daily >= 2% or hourly >= 1.5%
- Exit: trailing TP, profit lock, stop-loss, breakeven, stale rotation
- Price reads use hardcoded exchange='kraken' (correct)

### 3. PROJECT_ROOT/traders/trades/alpaca_stocks.py (530 lines)
Port of execute_cycle.py (Alpaca) modified for US stocks:
- EXCHANGE_NAME = "alpaca-stocks"
- Stocks only: NVDA, PLTR, TSLA, AMD, GOOGL, META, AAPL, MSFT, AMZN, AVGO
- PDT limit: max 3 trades/day
- Market hours check (Alpaca clock)
- time_in_force: "day" (NOT "gtc" for stocks)
- Fee: ~0.005% per side (not 0.5% for crypto)

## Review Criteria
1. **SQL correctness**: Do all DB queries use the right exchange identifier?
2. **Import paths**: Do all sys.path.inserts work from subdirectories?
3. **Error handling**: Are API failures, rate limits, and network issues handled?
4. **Race conditions**: Lock file usage correct?
5. **Sizing & risk**: Position sizing, stop-losses, max trade limits correct?
6. **Edge cases**: Empty balance, missing tickers, closed market (Alpaca)?
7. **API correctness**: CCXT parameter names, Alpaca API endpoints correct?
8. **Gate integration**: AI gate (ai_gate.json) check correct?
