# Restructure: 1 Strategy Per Script — Kraken + Alpaca Stocks

## Background

The aitrader project currently has all trading scripts in `traders/extreme/`. We're restructuring into two new directories:
- `traders/crypto_trades/` — all crypto trading scripts (Kraken only)
- `traders/trades/` — stock trading scripts (Alpaca only)

**Current problematic state:** Both strategy types (pullback + momentum) run from different exchanges with different API patterns, but all files are dumped in one directory. Alpaca does crypto (which should be migrated to Kraken) instead of stocks.

## Architecture Decisions

### Kraken crypto: 2 strategies, 2 scripts, same exchange
- Both Kraken strategies run every 5 minutes via separate cron jobs
- Each script is fully independent (no shared trading state between them)
- Each has its own lock file to prevent double-runs

### Alpaca becomes stock-only
- Remove all crypto logic
- Use Alpaca stock/bars endpoints
- Max 3 trades per day (self-imposed PDT protection)

### Shared infrastructure (remains in `traders/extreme/`)
- `db_prices.py` — DB helpers, trade logging, state management
- `ai_overseer.py` — AI evaluation (works on DB + gate file, exchange-agnostic)
- `ai_gate.json` — pause/resume flags (both scripts check this)
- `system_health_check.py` — system monitoring
- `fetch_news.py` — news fetching
- `weekly_rethink.py` — weekly analysis

## Existing Files

### Current Alpaca Momentum: `traders/extreme/execute_cycle.py` (583 lines)
- **Strategy:** Momentum breakout on crypto (BTC, ETH, SOL, AVAX, LINK)
- **Entry:** daily >= 2% OR hourly >= 1.5%
- **Sizing tiers:** 0.33/0.67/1.0 of max 50% position
- **Exit:** Trailing TP (+3% peak → give back 1%), Profit Lock (+5% peak → floor 3%), Stop-loss (-3.5%), Breakeven protection
- **Stale rotation:** sells positions held >30min if flat or >1h, rotates to stronger signal
- **API:** Alpaca REST (requests library)
- **Cron:** `run_extreme_cycle.sh` → called every 5 min

**What must happen:**
- Remove crypto pairs → replace with US stock list (NVDA, PLTR, TSLA, AMD, GOOGL, META, AAPL, MSFT, AMZN, etc.)
- Entry: daily >= 1.5% OR intraday >= 1.0% (stocks are less volatile)
- Exits: trailing TP, profit lock, stop-loss (-3%), breakeven protection
- Max 3 trades/UTC day (hard cap, enforced via DB trade_log COUNT)
- Use Alpaca stock snapshots/bars endpoints (different API path: `/v2/stocks/snapshots` or `/v1beta2/assets`)
- Remove stale rotation (not needed for small portfolio)
- Check stock market hours: only trade when market is open (Alpaca clock endpoint already gives this)
- Remove asset_type distinction (all stocks now)

### Current Kraken Pullback: `traders/extreme/execute_kraken_cycle_v2.py` (772 lines)
- **Strategy:** Pullback-in-uptrend
- **Entry:** Buy dip >= 0.5% below 1h high, inside 3h uptrend >= 1.0%, vol >= 3.0%
- **Exit:** Dynamic hard stop (2.5-8%), trailing TP (arm 1.5%, giveback 0.7%), TP cap 6%, max-hold dead-bag
- **API:** CCXT → Kraken
- **Cron:** `run_kraken_cycle.sh` → called every 5 min
- **Lock:** kraken_v2.lock (fcntl)

**Action:** Move to `crypto_trades/kraken_pullback.py`, fix imports, keep logic identical.

### New: Kraken Momentum (`crypto_trades/kraken_momentum.py`)
Port the momentum breakout strategy from Alpaca's execute_cycle.py to Kraken/CCXT.

**Strategy (ported from Alpaca):**
- Same crypto pairs: BTC/EUR, ETH/EUR, SOL/EUR, AVAX/EUR, LINK/EUR, etc.
- Fetch tickers via CCXT (like pullback does)
- Entry: daily >= 2.0% OR hourly >= 1.5% (using DB price history for daily change, get_one_hour_momentum for hourly)
- Size tiers: MODERATE (0.33), STRONG (0.67), EXTREME (1.0) of max position %
- Max position limit: must not exceed 5 open positions total (including pullback positions!)
- Sizing: similar to pullback (risk-based, ~97% deployment)

**Critical shared constraint:** Both Kraken strategies share the same pool of capital. The momentum script must check:
- That the coin is not already held by the pullback script
- Total open positions across BOTH strategies don't exceed safe limits
- Each needs its own lock file (`kraken_momentum.lock`)

**Exit for momentum (ported from Alpaca, adapted for CCXT):**
- Trailing TP: arm at +3% peak, sell if give back 1%
- Profit lock: arm at +5% peak, sell if drop below +3%  
- Stop-loss: -3.5%
- Breakeven protection: if peak >= +1% and now at fee floor
- Stale rotation: held >45min and flat (<1.0%), OR held >1.5h → rotate to stronger signal
- Max hold: 12h hard time-stop

**Exit implementation:** Use CCXT/Kraken market sells, log to DB via db_prices.log_trade()

**AI Gate:** Must check ai_gate.json (script_paused, consult_on_entry) — same as pullback but with its own gate key or shared. Use shared.

### Alpaca Stocks (`trades/alpaca_stocks.py`)
Modified from execute_cycle.py. Key changes:

**Universe (US stocks):**
```python
STOCK_SYMBOLS = ["NVDA", "PLTR", "TSLA", "AMD", "GOOGL", "META", "AAPL", "MSFT", "AMZN", "AVGO"]
```

**Entry:**
- Fetch snapshots via Alpaca data API: `GET /v2/stocks/snapshots?symbols=X,Y,Z`
- Compute daily change % (vs previous close, not open)
- Entry if: daily >= 1.5% OR intraday momentum >= 1.0%
- Size tiers: same 0.33/0.67/1.0 of max 50% position

**Exit:**
- Trailing TP: +3% peak → give back 1%
- Profit lock: +5% peak → floor 3%
- Stop-loss: -3%
- Breakeven: peak >= 1% and now at fee floor
- Max hold: 12h (but stocks mostly held intraday)

**Max 3 trades/day:**
```python
def trades_today(conn):
    cur.execute("SELECT COUNT(*) FROM trade_log WHERE exchange='alpaca' AND action='BUY' AND timestamp >= date_trunc('day', CURRENT_TIMESTAMP AT TIME ZONE 'UTC')")
    return int(cur.fetchone()[0])
```

**Market hours check:**
- Only trade when Alpaca clock says market is open (already in execute_cycle.py)
- Skip entry if market closed (exit still runs for stop-loss)
- Use `time_in_force: "day"` for stocks (not "gtc")

## File Operations

### Create directories:
- `traders/crypto_trades/` 
- `traders/trades/`

### Files to create:
1. `traders/crypto_trades/kraken_pullback.py` (copy from execute_kraken_cycle_v2.py, fix imports and paths, rename exchange name to "kraken-v2-pullback")
2. `traders/crypto_trades/kraken_momentum.py` (new: ported momentum strategy via CCXT)
3. `traders/trades/alpaca_stocks.py` (new: Alpaca stock momentum)

### Files to modify:
4. `~/.hermes/scripts/run_kraken_cycle.sh` → point to `traders/crypto_trades/kraken_pullback.py`
5. Create `~/.hermes/scripts/run_kraken_momentum.sh` → point to `traders/crypto_trades/kraken_momentum.py`
6. Create `~/.hermes/scripts/run_alpaca_stocks.sh` → point to `traders/trades/alpaca_stocks.py`
7. Update or create cron jobs:
   - `run_extreme_cycle.sh` (current Alpaca crypto) → disable or replace with `run_alpaca_stocks.sh`
   - `run_kraken_cycle.sh` → keep but update path
   - New: `run_kraken_momentum.sh` → every 5 min

### Old files (KEEP in place as legacy, do NOT delete):
- `traders/extreme/execute_cycle.py` — legacy, keep
- `traders/extreme/execute_kraken_cycle_v2.py` — legacy copy, keep

**But** update `.gitignore` or handle them — they'll show as untracked duplicates.

## Git Workflow
After all changes:
1. `git status` — show what changed
2. `git add` all new files
3. `git commit -m "refactor: 1-strategy-per-script architecture · Kraken (pullback+momentum) + Alpaca stocks"`
4. `git push origin configurable-runners`

## Cron Jobs to Update

**Current state:**
| Cron Name | Script | Schedule | Exchange | Strategy |
|-----------|--------|----------|----------|----------|
| AITrader 24/7 Crypto | run_extreme_cycle.sh | */5 min | Alpaca | Crypto momentum |
| AITrader Kraken 24/7 Crypto | run_kraken_cycle.sh | */5 min | Kraken | Pullback v2 |

**Target state:**
| Cron Name | Script | Schedule | Exchange | Strategy |
|-----------|--------|----------|----------|----------|
| Kraken Pullback | run_kraken_pullback.sh | */5 min | Kraken | Pullback v2 |
| Kraken Momentum | run_kraken_momentum.sh | */5 min | Kraken | Momentum breakout |
| Alpaca Stocks | run_alpaca_stocks.sh | */5 min (market hours) | Alpaca | Stock momentum (max 3/day) |

## IMPORTANT Implementation Notes

1. **DB schema stays the same** — `db_prices.py` in `traders/extreme/` is not moved. All new scripts import from it via `sys.path.append` or relative imports.
2. **Each script must have its own lock file** — fcntl flock() protection
3. **Kraken momentum must track positions separately from pullback** — they share the same exchange wallet but different trading_state keys. Use separate state keys like "kraken-pullback" and "kraken-momentum".
4. **Do NOT delete old files** — just create the new ones and update cron scripts
5. **Run `python3 -c "import ast; ast.parse(open('file').read())"` after creating every .py file** to validate syntax
6. **Environment variables** are loaded from `PROJECT_ROOT/.env` using `load_dotenv`
7. **All file paths** must be absolute (the cron jobs cd to `PROJECT_ROOT`)
