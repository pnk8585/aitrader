# Kraken Momentum bot: MIN LOT fix

**File:** `PROJECT_ROOT/traders/crypto_trades/kraken_momentum.py`

**Error:**
```
BUY_FAILED: Failed to buy RENDER/EUR: kraken {"error":["EGeneral:Invalid arguments:volume minimum not met"]}
```

The momentum bot is trying to buy with a quantity below Kraken's minimum lot size for RENDER/EUR.

## Context

The pullback bot (`kraken_pullback.py`) already has this fixed (commit 1915aa0). Look at how it handles the pre-flight min lot check.

The buy logic is around line 900-910 of `kraken_momentum.py`:
```python
qty = order_size_eur / current_price
...
exchange.load_markets()
fqty = float(exchange.amount_to_precision(symbol, qty))
res = exchange.create_market_buy_order(symbol, fqty)
```

The fix should:
1. After calculating `qty`, check `exchange.markets[symbol]['limits']['amount']['min']` 
2. If `qty < min_lot`, log it and skip — don't submit the order
3. The fix should be EXACTLY like the one in `kraken_pullback.py` (same pattern)

## Task
1. Read `kraken_pullback.py` to find the min lot fix pattern
2. Apply the same fix to `kraken_momentum.py` at the buy site
3. Verify syntax
4. No commit — I'll handle that
