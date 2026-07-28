# AITrader Implementation Plan
> Strategy upgrades: regime detection, grid, Kelly, ATR stops, laddered TP, DCA
> Created: 2026-07-28 | Status: PLANNING

---

## Architecture Overview

```
                    ┌──────────────────────┐
                    │   REGIME DETECTOR    │  ← NEW: runs every 5min
                    │   (shared module)    │
                    │   writes to DB       │
                    └──────┬───────────────┘
                           │ regime column in asset_prices
              ┌────────────┼────────────┐
              ▼            ▼            ▼
         ┌─────────┐ ┌─────────┐ ┌──────────┐
         │MOMENTUM │ │  GRID   │ │ PULLBACK │
         │(exists) │ │  (NEW)  │ │(exists)  │
         │extended │ │         │ │extended  │
         └────┬────┘ └────┬────┘ └────┬─────┘
              │           │           │
              └───────────┼───────────┘
                          ▼
               ┌──────────────────────┐
               │  SHARED EXIT LOGIC   │  ← NEW: ATR stops, laddered TP
               │  traders/common/     │
               └──────────┬───────────┘
                          ▼
               ┌──────────────────────┐
               │  POSITION MONITOR    │  (exists, catch-all)
               │  + Kelly sizing      │  ← EXTENDED
               └──────────────────────┘
```

---

## PHASE 1: Shared Foundation (Week 1)
> Κανένα breaking change. Νέα modules, δεν πειράζουμε υπάρχοντα.

### 1A. Regime Detector Module
**New file:** `traders/strategies/regime/detector.py`
**New file:** `traders/strategies/regime/config.py`

```python
# Conceptual code — regime/detector.py
def detect_regime(db_conn, symbol: str) -> str:
    """Returns: 'trending' | 'ranging' | 'crisis' | 'uncertain'"""
    # Read last 20 days of prices from asset_prices table
    # Compute: ADX(14), 20d volatility, 20d return
    # Rules-based classification
    if vol_20d > 30:
        return "crisis"
    elif adx > 25 and abs(ret_20d) > 5:
        return "trending"
    elif adx < 20 and vol_20d < 15:
        return "ranging"
    return "uncertain"
```

**Storage:** New column `regime TEXT` in `asset_prices` table (or new table `regime_state`)
**Schedule:** Part of existing `kraken-momentum` 5min cycle — computes regime BEFORE entry logic
**Cron impact:** NONE — regime detection runs INSIDE existing cron jobs, not a new cron

### 1B. ATR-Based Stop Module
**New file:** `traders/common/atr_stops.py`

```python
def compute_atr_stop(entry_price: float, atr: float, multiplier: float = 2.0) -> float:
    """Replace fixed -% stop with ATR-based stop."""
    return entry_price - (atr * multiplier)

def compute_atr_tp(entry_price: float, atr: float, multiplier: float = 3.0) -> float:
    """ATR-based take-profit target."""
    return entry_price + (atr * multiplier)

def should_move_to_breakeven(current_price, entry_price, atr, threshold_mult=2.0):
    """Move stop to breakeven after price moves 2×ATR in our favor."""
    return current_price > entry_price + (atr * threshold_mult)
```

**Extends:** `traders/strategies/pullback/exits.py` and `momentum/exits.py`
**How:** Add ATR stop as OPTION alongside existing fixed stop. Config flag: `USE_ATR_STOPS = True`

### 1C. Kelly Sizing Module
**New file:** `traders/common/kelly.py`

```python
def kelly_fraction(win_rate: float, avg_win: float, avg_loss: float) -> float:
    """Full Kelly fraction. Returns 0-1."""
    if avg_loss == 0:
        return 0
    b = avg_win / avg_loss
    f = (b * win_rate - (1 - win_rate)) / b
    return max(0, f)

def kelly_position_size(db_conn, exchange_name: str, entry: float, stop: float,
                        balance: float, fraction: float = 0.25) -> float:
    """Quarter-Kelly position sizing from trade_log history."""
    # Query last 50 trades from trade_log
    # Compute win_rate, avg_win_pct, avg_loss_pct
    # Apply Kelly formula
    # Cap at fraction (default quarter-Kelly)
    # Size inversely proportional to stop distance
```

**Extends:** Entry logic in both `kraken_momentum.py` and `kraken_pullback.py`
**How:** Replace `DEPLOY_FRACTION` with `kelly_position_size()` call. Fallback to fixed fraction if < 20 trades.

### 1D. Laddered Take-Profit Module
**New file:** `traders/common/laddered_tp.py`

```python
# Default ladders (configurable per strategy)
DEFAULT_LADDERS = [
    {"trigger_pct": 5.0,  "sell_fraction": 0.25},
    {"trigger_pct": 10.0, "sell_fraction": 0.25},
    {"trigger_pct": 15.0, "sell_fraction": 0.25},
    {"trigger_pct": 25.0, "sell_fraction": 0.25},
]

def get_next_tp_level(position, ladders=DEFAULT_LADDERS):
    """Which TP level to check next, based on already-sold fractions."""
    # Track in trading_state: tp_level INT DEFAULT 0
    pass

def should_take_partial_profit(unrealized_plpc, tp_level, ladders=DEFAULT_LADDERS):
    """Returns (should_sell, sell_fraction, reason)"""
    pass
```

**DB change:** Add `tp_level INT DEFAULT 0` column to `trading_state`
**Extends:** Both exit files — after checking stop, check laddered TP before hold
**Cron impact:** NONE — runs inside existing strategy cycles

### Phase 1 Files Summary
| File | Type | Action |
|------|------|--------|
| `traders/strategies/regime/detector.py` | NEW | Regime classification |
| `traders/strategies/regime/config.py` | NEW | Regime thresholds |
| `traders/strategies/regime/__init__.py` | NEW | Package |
| `traders/common/atr_stops.py` | NEW | ATR stop/TP utilities |
| `traders/common/kelly.py` | NEW | Kelly sizing |
| `traders/common/laddered_tp.py` | NEW | Partial profit logic |
| `traders/strategies/pullback/exits.py` | MODIFY | Wire ATR stops + laddered TP |
| `traders/strategies/pullback/config.py` | MODIFY | Add ATR/Kelly config flags |
| `traders/strategies/momentum/exits.py` | MODIFY | Wire ATR stops + laddered TP |
| `traders/strategies/momentum/config.py` | MODIFY | Add ATR/Kelly config flags |
| `traders/crypto_trades/kraken_momentum.py` | MODIFY | Use regime + Kelly for entry |
| `traders/crypto_trades/kraken_pullback.py` | MODIFY | Use regime + Kelly for entry |
| `position_monitor.py` | MODIFY | Kelly sizing fallback |

### Phase 1 DB Migration
```sql
-- Regime state table
CREATE TABLE IF NOT EXISTS regime_state (
    id SERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    regime TEXT NOT NULL,  -- 'trending','ranging','crisis','uncertain'
    adx_14 NUMERIC,
    vol_20d NUMERIC,
    ret_20d NUMERIC,
    computed_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_regime_symbol ON regime_state(symbol, computed_at DESC);

-- Laddered TP tracking
ALTER TABLE trading_state ADD COLUMN IF NOT EXISTS tp_level INT DEFAULT 0;
ALTER TABLE trading_state ADD COLUMN IF NOT EXISTS tp_sold_qty NUMERIC DEFAULT 0;

-- Regime column on asset_prices (optional, for dashboard)
ALTER TABLE asset_prices ADD COLUMN IF NOT EXISTS regime TEXT;
```

### Phase 1 Cron Impact
**ZERO new crons.** All modules plug into existing cycles:
- Regime: computed inside `kraken-momentum` cycle (5min), cached in DB
- ATR stops: checked inside existing exit logic (same cycle)
- Kelly: computed on entry (same cycle)
- Laddered TP: checked inside existing exit logic (same cycle)

---

## PHASE 2: Grid Trading (Week 2)
> Νέα strategy, νέο cron job. Δεν πειράζει υπάρχοντα.

### 2A. Grid Strategy Module
**New file:** `traders/strategies/grid/config.py`
**New file:** `traders/strategies/grid/engine.py`
**New file:** `traders/crypto_trades/kraken_grid.py`

```python
# Conceptual: grid/engine.py
class GridEngine:
    def __init__(self, symbol, low_price, high_price, num_grids, total_capital):
        self.levels = self._compute_levels(low_price, high_price, num_grids)
        self.capital_per_grid = total_capital / num_grids
    
    def _compute_levels(self, low, high, n):
        step = (high - low) / n
        return [low + i * step for i in range(n + 1)]
    
    def get_open_orders(self, current_price, held_qty):
        """Returns list of {action, price, qty} for open grid levels."""
        pass
    
    def should_activate(self, regime):
        """Only active when regime = 'ranging'."""
        return regime == "ranging"
```

### 2B. Grid Cron Job
**New cron:** `kraken-grid` — every 5min, same as momentum/pullback
**DB entry:**
```sql
INSERT INTO cron_jobs (name, schedule_seconds, mode, enabled, next_run_at, updated_at)
VALUES ('kraken-grid', 300, 'paper', TRUE, NOW(), NOW());
```

**Mode:** Starts in `paper` mode. Switch to `live` after backtesting.

### 2C. Grid + Regime Integration
The grid strategy reads `regime_state` table:
- If `regime = 'ranging'` → activate grid, deactivate momentum entries
- If `regime = 'trending'` → deactivate grid, let momentum run
- If `regime = 'uncertain'` → grid runs conservatively (wider range)

### 2D. Auto-Range Detection
```python
def compute_grid_range(symbol, lookback_days=30):
    """Auto-compute grid range from 30-day support/resistance."""
    # Read prices from asset_prices
    # low = 5th percentile, high = 95th percentile
    # Add 5% buffer on each side
    pass
```

### Phase 2 Files Summary
| File | Type | Action |
|------|------|--------|
| `traders/strategies/grid/config.py` | NEW | Grid parameters |
| `traders/strategies/grid/engine.py` | NEW | Core grid logic |
| `traders/strategies/grid/__init__.py` | NEW | Package |
| `traders/crypto_trades/kraken_grid.py` | NEW | Cron entry point |
| `app/cron_orchestrator.py` | MODIFY | Register kraken-grid |

### Phase 2 Cron Schedule
```
kraken-grid: every 300s (5min), mode=paper initially
```

---

## PHASE 3: Regime-Aware Strategy Routing (Week 3)
> Ενεργοποίηση του regime detector ως router.

### 3A. Strategy Router
**New file:** `traders/strategies/regime/router.py`

```python
def get_active_strategies(regime: str) -> dict:
    """Returns which strategies should be active for this regime."""
    if regime == "trending":
        return {"momentum": True, "pullback": True, "grid": False}
    elif regime == "ranging":
        return {"momentum": False, "pullback": False, "grid": True}
    elif regime == "crisis":
        return {"momentum": False, "pullback": False, "grid": False}
    else:  # uncertain
        return {"momentum": True, "pullback": True, "grid": False}
```

### 3B. Strategy Gate in Each Cron
Each strategy cron checks regime before running entry logic:
```python
# In kraken_momentum.py run_cycle():
regime = detect_regime(db, pair)
router = get_active_strategies(regime)
if not router.get("momentum"):
    report["details"] = f"Skipped: regime={regime}, momentum inactive"
    return
```

**Key:** EXIT logic always runs regardless of regime. Only ENTRY is gated.

### 3C. Dashboard Regime Display
**Modify:** `app/templates/dashboard.html` — show current regime per coin
**Modify:** `app/main.py` — add regime to open_positions context

### Phase 3 Files Summary
| File | Type | Action |
|------|------|--------|
| `traders/strategies/regime/router.py` | NEW | Strategy routing logic |
| `traders/crypto_trades/kraken_momentum.py` | MODIFY | Gate entries on regime |
| `traders/crypto_trades/kraken_pullback.py` | MODIFY | Gate entries on regime |
| `traders/crypto_trades/kraken_grid.py` | MODIFY | Gate entries on regime |
| `app/main.py` | MODIFY | Add regime to dashboard |
| `app/templates/dashboard.html` | MODIFY | Show regime badge |

### Phase 3 Cron Impact
**ZERO new crons.** Regime routing is inside existing cycles.

---

## PHASE 4: DCA Entry Enhancement (Week 4)
> Επέκταση του entry logic.

### 4A. DCA Entry Module
**New file:** `traders/common/dca_entry.py`

```python
DCA_LEVELS = [
    {"drop_pct": 0,   "deploy_pct": 0.50},  # 50% on signal
    {"drop_pct": 3.0, "deploy_pct": 0.25},  # 25% if drops 3%
    {"drop_pct": 6.0, "deploy_pct": 0.25},  # 25% if drops 6%
]

def dca_entry_decision(signal_price, current_price, dca_level, levels=DCA_LEVELS):
    """Decide if we should buy more at this DCA level."""
    drop_pct = (signal_price - current_price) / signal_price * 100
    level = levels[dca_level]
    if drop_pct >= level["drop_pct"]:
        return level["deploy_pct"]
    return 0
```

### 4B. DCA State Tracking
**DB change:**
```sql
ALTER TABLE trading_state ADD COLUMN IF NOT EXISTS dca_level INT DEFAULT 0;
ALTER TABLE trading_state ADD COLUMN IF NOT EXISTS signal_price NUMERIC;
```

### 4C. Integration
**Modify:** `kraken_momentum.py` and `kraken_pullback.py` entry logic
- On first entry: deploy 50%, record `signal_price` and `dca_level=0`
- On subsequent cycles: check if price dropped enough for DCA level 1 or 2
- Cap total position at original max

### Phase 4 Cron Impact
**ZERO new crons.** DCA checks happen inside existing 5min cycles.

---

## PHASE 5: Backtesting & Optimization (Week 5+)
> Επαλήθευση ότι δουλεύουν.

### 5A. Trade Logger Enhancement
**Modify:** `traders/common/db_log_trade.py` — log regime + strategy + ATR at entry
**DB change:**
```sql
ALTER TABLE trade_log ADD COLUMN IF NOT EXISTS regime TEXT;
ALTER TABLE trade_log ADD COLUMN IF NOT EXISTS atr_at_entry NUMERIC;
ALTER TABLE trade_log ADD COLUMN IF NOT EXISTS kelly_fraction NUMERIC;
ALTER TABLE trade_log ADD COLUMN IF NOT EXISTS strategy_name TEXT;
```

### 5B. Backtest Runner
**New file:** `scripts/backtest_strategy.py`
- Replays historical `asset_prices` data
- Simulates each strategy with current config
- Reports: win rate, avg P&L, max drawdown, Sharpe ratio
- Compares: fixed stop vs ATR stop, fixed sizing vs Kelly

### 5C. Parameter Optimization (Inspired by Freqtrade HyperOpt)
**New file:** `scripts/optimize_params.py`
- Grid search over key parameters:
  - `TREND_3H_MIN_PCT`: 2.0 - 5.0
  - `PULLBACK_MIN_PCT`: 2.0 - 5.0
  - `ATR_MULTIPLIER`: 1.5 - 3.0
  - `KELLY_FRACTION`: 0.10 - 0.50
- Uses backtest runner to evaluate each combo
- Reports best parameter set

---

## Complete Cron Schedule (After All Phases)

| Job | Interval | Mode | What It Does |
|-----|----------|------|-------------|
| `kraken-momentum` | 5min | live | Scan momentum signals + regime detection + ATR/Kelly entry |
| `kraken-pullback` | 5min | live | Scan pullback signals + regime detection + ATR/Kelly entry |
| `kraken-grid` | 5min | paper→live | Grid trading in ranging markets |
| `position-monitor` | 2h | live | Catch-all exits + laddered TP + ATR stops |
| `alpaca-stocks` | 5min | live | US stocks (unchanged) |
| `db-cleanup` | 24h | live | Cleanup (unchanged) |
| `end-of-day-review` | 24h | live | Daily summary (unchanged) |

**No new crons except `kraken-grid`.** All other changes plug into existing cycles.

---

## Migration Path (No Breaking Changes)

```
Phase 1 ──→ Phase 2 ──→ Phase 3 ──→ Phase 4 ──→ Phase 5
modules    grid cron    routing     DCA entry    backtest
only       (paper)      (regime)    enhance      & optimize
           │            │           │
           │            │           └─ all behind config flags
           │            └─ behind USE_REGIME_ROUTING flag
           └─ starts paper, no live impact
```

**Every phase is behind a config flag.** Can be enabled/disabled per strategy:
```python
# In each strategy's config.py
USE_ATR_STOPS = True       # Phase 1
USE_KELLY_SIZING = True    # Phase 1
USE_LADDERED_TP = True     # Phase 1
USE_REGIME_ROUTING = False  # Phase 3 (enable after testing)
USE_DCA_ENTRY = False       # Phase 4 (enable after testing)
```

---

## Risk Assessment

| Risk | Mitigation |
|------|-----------|
| ATR stops wider than expected → bigger losses | Cap at MAX_HARD_STOP_PCT (existing) |
| Kelly over-sizes on small sample | Fallback to fixed fraction if < 20 trades |
| Grid bot in trending market → bags | Regime gate: only active in ranging |
| Regime misclassification → wrong strategy | Default to "uncertain" → both active |
| DCA averaging into falling knife | Max 3 levels, total position cap |
| Laddered TP sells too early in big run | Last 25% uses trailing stop, not fixed TP |

---

## Dashboard UI Changes

### New Elements
1. **Regime badge** per coin on dashboard (color-coded: green=trending, yellow=ranging, red=crisis)
2. **Active strategy indicator** showing which strategy is handling each position
3. **TP level progress** bar on open positions
4. **Kelly fraction** display on last trade
5. **ATR stop** display instead of fixed %

### Files to Modify
- `app/templates/dashboard.html`
- `app/templates/_dash_positions.html` (if exists)
- `app/main.py` (context enrichment)

---

## Summary: What's New vs What Changes

### Brand New (no impact on existing)
- `traders/strategies/regime/` — detector, config, router
- `traders/common/atr_stops.py` — ATR utilities
- `traders/common/kelly.py` — Kelly sizing
- `traders/common/laddered_tp.py` — partial profit
- `traders/common/dca_entry.py` — DCA logic
- `traders/strategies/grid/` — grid engine
- `traders/crypto_trades/kraken_grid.py` — grid cron
- `scripts/backtest_strategy.py` — backtester
- `scripts/optimize_params.py` — parameter optimizer

### Extended (behind config flags)
- `traders/strategies/pullback/exits.py` — ATR stops + laddered TP
- `traders/strategies/pullback/config.py` — new config flags
- `traders/strategies/momentum/exits.py` — ATR stops + laddered TP
- `traders/strategies/momentum/config.py` — new config flags
- `traders/crypto_trades/kraken_momentum.py` — regime + Kelly + DCA
- `traders/crypto_trades/kraken_pullback.py` — regime + Kelly + DCA
- `position_monitor.py` — Kelly fallback
- `app/main.py` — regime on dashboard
- `app/templates/dashboard.html` — regime badge + TP progress

### New Cron Jobs
- **Only 1:** `kraken-grid` (5min, starts in paper mode)

### DB Migrations
- `regime_state` table
- `tp_level`, `tp_sold_qty` columns on `trading_state`
- `dca_level`, `signal_price` columns on `trading_state`
- `regime`, `atr_at_entry`, `kelly_fraction`, `strategy_name` on `trade_log`
