# Phase 2: Grid Trading Strategy — SPECIFICATION (Pre-Implementation Review)

## Context

Phase 1 (merged) added: regime detector, ATR stops, Kelly sizing, laddered TP — all behind config flags. Phase 2 adds a **Grid Trading strategy** that profits from sideways/ranging markets where Momentum and Pullback sit idle.

The regime detector from Phase 1 will eventually route to Grid when `regime='ranging'`, but Phase 2 builds the grid engine standalone. Regime routing is Phase 3.

---

## 1. Architecture

### New Files
```
traders/strategies/grid/
├── __init__.py
├── config.py          # Grid parameters (pairs, grid count, range %, etc.)
├── engine.py          # Core grid logic (level computation, order generation)
traders/crypto_trades/
├── kraken_grid.py     # Cron entry point (same pattern as kraken_momentum.py)
scripts/
├── phase2_migration.sql  # DB schema for grid state
```

### Modified Files
```
app/cron_orchestrator.py   # Register kraken-grid in JOB_REGISTRY
```

### New Cron Job
- **Name:** `kraken-grid`
- **Interval:** 300s (5 minutes) — same as momentum/pullback
- **Default mode:** `paper` — switch to live after backtesting
- **Script:** `traders/crypto_trades/kraken_grid.py`

---

## 2. Grid Engine Design

### Core Concept
A grid divides a price range into N evenly-spaced levels. Buy orders below current price, sell orders above. Each completed buy→sell cycle captures the grid spread as profit.

### State Management
Grid state must persist between 5-min cycles. Use a new `grid_state` table:

```sql
CREATE TABLE IF NOT EXISTS grid_state (
    id SERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL DEFAULT 'kraken',
    grid_low NUMERIC NOT NULL,
    grid_high NUMERIC NOT NULL,
    num_grids INTEGER NOT NULL DEFAULT 10,
    capital_allocated NUMERIC NOT NULL,
    -- Per-level tracking: array of {level_price, status, buy_qty, buy_price, sell_price}
    levels JSONB NOT NULL DEFAULT '[]',
    -- Aggregate
    total_buys INTEGER DEFAULT 0,
    total_sells INTEGER DEFAULT 0,
    realized_pnl NUMERIC DEFAULT 0,
    -- Lifecycle
    status TEXT NOT NULL DEFAULT 'active',  -- active | paused | stopped
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(symbol, exchange)
);
```

### Grid Level States
Each grid level has a lifecycle:
```
IDLE → BUY_PLACED → BUY_FILLED → SELL_PLACED → SELL_FILLED → IDLE (cycle complete)
```

Stored as JSONB in `grid_state.levels`:
```json
[
  {"price": 5.50, "status": "idle", "buy_qty": 0, "buy_price": null, "sell_price": null},
  {"price": 5.60, "status": "buy_filled", "buy_qty": 1.2, "buy_price": 5.60, "sell_price": null},
  ...
]
```

### Auto-Range Detection
When creating a new grid for a symbol:
1. Query `asset_prices` for last 30 days of daily closes
2. `grid_low = percentile(prices, 5) * 0.95` (5% buffer below)
3. `grid_high = percentile(prices, 95) * 1.05` (5% buffer above)
4. If range < 8% → skip (too tight for grid to be profitable after fees)
5. If range > 60% → cap at 60% (extreme volatility, too risky)

### Grid Sizing
- Total capital per grid: `cash_eur / MAX_OPEN_GRIDS` (default MAX_OPEN_GRIDS=3)
- Capital per level: `total_capital / num_grids`
- Qty per level: `capital_per_level / level_price`
- Minimum trade: €5 per level (same as momentum MIN_TRADE_EUR)

### Fee Handling
- Kraken maker: 0.16%, taker: 0.26%
- Grid spread must be > ROUND_TRIP_FEE_PCT (0.52%) per level to be profitable
- If `grid_spread < 0.7%` → reduce num_grids to ensure profitability

---

## 3. Cycle Logic (kraken_grid.py — every 5 min)

```python
def run_cycle():
    db = get_connection()
    
    for pair in CRYPTO_PAIRS:
        # 1. Check if grid exists for this pair
        grid = load_grid(db, pair)
        
        if grid is None:
            # No grid yet — check if we should create one
            regime = detect_regime(db, pair)  # Phase 1 module
            if regime == 'ranging' or not USE_REGIME_ROUTING:
                grid = create_grid(db, pair)
            continue
        
        if grid['status'] != 'active':
            continue
        
        # 2. Get current price
        price = get_current_price(pair)
        
        # 3. Check each level
        for level in grid['levels']:
            if level['status'] == 'idle':
                # Place buy order if price is near this level
                if price <= level['price'] * 1.005:  # within 0.5%
                    place_grid_buy(db, grid, level, pair, price)
            
            elif level['status'] == 'buy_filled':
                # Place sell order at grid_spread above buy
                sell_price = level['buy_price'] * (1 + grid_spread_pct)
                if price >= sell_price * 0.995:
                    place_grid_sell(db, grid, level, pair, sell_price)
            
            elif level['status'] == 'sell_filled':
                # Cycle complete — reset to idle
                reset_level(db, grid, level)
        
        # 4. Check if grid range is still valid (price drifted)
        check_and_adjust_range(db, grid, pair, price)
    
    db.close()
```

### Grid Rebalancing
If price moves outside the grid range:
- **Below grid_low:** Grid is fully bought, no sells happening → extend range down by 10%
- **Above grid_high:** Grid is fully idle, no buys happening → extend range up by 10%
- **Gradual drift:** Adjust grid center toward current price every 24h

### Grid Stop-Loss
If a grid position drops >15% from its buy price (same as position-monitor hard stop):
- Sell the position
- Mark level as `stopped`
- If >50% of levels are stopped → pause entire grid, alert via Telegram

---

## 4. Config (traders/strategies/grid/config.py)

```python
CRYPTO_PAIRS = [
    "BTC/EUR", "ETH/EUR", "SOL/EUR", "AVAX/EUR", "LINK/EUR",
    "XRP/EUR", "DOGE/EUR", "SUI/EUR", "NEAR/EUR", "RENDER/EUR",
    "ADA/EUR", "DOT/EUR",
]

EXCHANGE_NAME = "kraken-grid"
PRICE_EXCHANGE = "kraken"

NUM_GRIDS = 10              # Number of grid levels (reduced for small accounts)
RANGE_LOOKBACK_DAYS = 30    # Days for auto-range calculation
RANGE_MIN_PCT = 8.0         # Minimum grid range %
RANGE_MAX_PCT = 60.0        # Maximum grid range %
RANGE_BUFFER_PCT = 5.0      # Buffer on each side

MAX_OPEN_GRIDS = 3          # Max simultaneous grids (symbols)
CAPITAL_PER_GRID_PCT = 0.33 # 1/3 of cash per grid
MIN_TRADE_EUR = 5.0         # Minimum per grid level
GRID_HARD_STOP_PCT = -15.0  # Hard stop per grid position

ROUND_TRIP_FEE_PCT = 0.52
MIN_GRID_SPREAD_PCT = 0.7   # Minimum spread per level (after fees)

REBALANCE_INTERVAL_HOURS = 24
EXTEND_RANGE_PCT = 10.0     # How much to extend when price exits range

USE_REGIME_ROUTING = False   # Phase 3 will enable this

LOCK_FILE = os.path.join(LOG_DIR, "kraken_grid.lock")
COOLDOWN_MIN = 0             # Grid has no cooldown (continuous)
MAX_TRADES_PER_DAY = 100     # Grid can make many small trades
```

---

## 5. DB Migration (scripts/phase2_migration.sql)

```sql
CREATE TABLE IF NOT EXISTS grid_state (
    id SERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL DEFAULT 'kraken',
    grid_low NUMERIC NOT NULL,
    grid_high NUMERIC NOT NULL,
    num_grids INTEGER NOT NULL DEFAULT 10,
    capital_allocated NUMERIC NOT NULL,
    levels JSONB NOT NULL DEFAULT '[]',
    total_buys INTEGER DEFAULT 0,
    total_sells INTEGER DEFAULT 0,
    realized_pnl NUMERIC DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(symbol, exchange)
);

CREATE INDEX IF NOT EXISTS idx_grid_state_symbol ON grid_state(symbol, exchange);
CREATE INDEX IF NOT EXISTS idx_grid_state_status ON grid_state(status);
```

---

## 6. Integration with Existing System

### No Touch on Existing Strategies
Grid is completely standalone. It:
- Uses its own `exchange_name='kraken-grid'` (paper: `'paper-kraken-grid'`)
- Tracks state in its own `grid_state` table
- Logs trades to `trade_log` with its own exchange prefix
- Does NOT interfere with momentum or pullback positions

### Shared Resources
- Uses `asset_prices` table for historical price data (read-only)
- Uses `ccxt.kraken` for order execution (same credentials)
- Uses `trading_state` for dashboard display (writes grid positions there too)
- Uses `trade_log` for trade logging

### Position Monitor Interaction
The position-monitor (2h cycle) sees ALL positions including grid positions. If a grid buy fills but the sell hasn't triggered yet, the position-monitor might try to evaluate it. Solution: position-monitor should skip positions with `exchange_name LIKE '%grid%'` — or better, grid manages its own exits exclusively.

**Decision needed:** Should position-monitor ignore grid positions entirely, or should it apply its -15% hard stop as a safety net?

### Dashboard
Grid positions should appear in the Open Positions table like any other position. The `exchange` column will show `kraken-grid` to distinguish them.

---

## 7. Telegram Notifications

Grid should notify on:
- Grid created: `🔲 Grid created: AVAX/EUR €5.20-€6.80, 15 levels, €5.67 allocated`
- Cycle completed (buy+sell): `💰 Grid cycle: AVAX/EUR bought @€5.60 sold @€5.70 (+1.78%, +€0.10)`
- Grid stopped (hard stop): `🛑 Grid stopped: AVAX/EUR — 3/15 levels hit hard stop`
- Grid rebalanced: `📏 Grid rebalanced: AVAX/EUR range extended to €4.90-€7.10`

Notify via existing `app.notify.send_telegram()`.

---

## 8. Safety Guards

1. **Max open grids:** 3 symbols simultaneously (prevents over-diversification)
2. **Capital cap:** Max 33% of cash per grid
3. **Hard stop:** -15% per grid position (same as position-monitor)
4. **Min spread:** Grid levels must be >0.7% apart (profitable after fees)
5. **Min range:** Grid range must be >8% (worth the complexity)
6. **Lock file:** Prevent concurrent grid cycles (same pattern as other strategies)
7. **Paper mode:** Starts in paper, switch to live via cron_jobs.mode

---

## 9. Files to Create (Implementation Order)

1. `scripts/phase2_migration.sql` — Run first
2. `traders/strategies/grid/__init__.py` — Empty
3. `traders/strategies/grid/config.py` — All constants
4. `traders/strategies/grid/engine.py` — Core grid logic (create, tick, rebalance, stop)
5. `traders/crypto_trades/kraken_grid.py` — Cron entry point
6. `app/cron_orchestrator.py` — Add `kraken-grid` to JOB_REGISTRY

---

## 10. Testing Plan

1. **Paper mode:** Run grid in paper mode for 48h, verify cycles complete
2. **Fee check:** Ensure each grid level profit > round-trip fee
3. **Range check:** Verify auto-range uses 30-day percentiles correctly
4. **Rebalance:** Simulate price exiting range, verify extension works
5. **Hard stop:** Simulate -15% drop, verify position sells and level stops
6. **Concurrent:** Run 3 grids simultaneously, verify no lock conflicts

---

## Decisions

1. **Position-monitor interaction:** YES — grid positions exempt from position-monitor's exit logic. Grid manages its own exits.
2. **Order type:** Limit orders (maker fee 0.16%) for better fills. Retry with market order if unfilled after 2 cycles.
3. **Grid count:** 10 levels (reduced from 15 for small €17 accounts — still profitable with min €5/level).
4. **Regime gating:** Disabled in Phase 2 (`USE_REGIME_ROUTING=False`). Phase 3 enables regime → grid routing.
