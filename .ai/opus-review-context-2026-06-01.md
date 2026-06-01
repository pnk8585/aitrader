# Full Context for Claude Code Opus Review

## Mission
Review ALL code we just built: **AI Overseer** (hourly agent) + **AI Gate System** + **v2 Kraken strategy** with the fixes from yesterday. Make improvements wherever you see fit — structural, safety, logic, edge cases, anything.

**BE AGGRESSIVE.** The bot has lost -9.91% (43.61€ → 33.75€). We need every edge.

---

## Repository
- **Path:** `PROJECT_ROOT`
- **Branch:** `configurable-runners`
- **Git:** clean, pushed to `origin/configurable-runners`
- **DB:** PostgreSQL (`trading` db). Tables: `asset_prices`, `trading_state`, `notify_state`, `trade_log`
- **Exchange:** Kraken (live, real money, paper trading removed)

---

## Architecture Overview

### 5-min Bot (`execute_kraken_cycle_v2.py`)
Path: `traders/extreme/execute_kraken_cycle_v2.py` (636 lines)

Primary trading bot. Runs every 5 minutes via cron (shell script `run_kraken_cycle.sh`).

**Strategy: pullback-in-uptrend**
1. Fetch tickers + balances, persist prices to DB
2. Manage existing positions (dynamic stop, trailing TP, take-profit cap, max-hold dead-bag exit)
3. Risk gates (max positions, buying power, daily trade cap, daily loss breaker)
4. **AI Gate check** (NEW) — reads `ai_overseer/ai_gate.json`, skips if `script_paused`, logs if `consult_on_entry`
5. Entry scan: for each of 12 crypto pairs:
   - Cooldown check (90 min per coin after exit)
   - Volatility floor (6h range >= 3.0%)
   - Higher-TF uptrend (3h >= +1.0%, 6h > 0%)
   - Anti blow-off guard (< +4% 1h momentum)
   - Pullback (>= 0.5% below 1h high)
   - **Bounce gate** (NEW Opus fix) — rejects if price still falling AND at 15min low
   - Score candidates, buy best
6. Hourly heartbeat notification

**Key constants (currently set by AI Overseer):**
- `VOL_FLOOR_PCT = 3.0` (was 1.8 — fixed)
- `MIN_HARD_STOP_PCT = 2.5` (was 2.0 — AI-adjusted from 2.5)
- `TRAIL_ARM_PCT = 1.5` (was 1.2 — AI-adjusted)
- `TRAIL_GIVEBACK_PCT = 0.7` (was 0.5 — AI-adjusted)
- `DEPLOY_FRACTION = 0.97` (97% in one trade)
- `MAX_OPEN_SMALL = 1`, `MAX_OPEN_LARGE = 2`
- `HARD_TP_CAP_PCT = 6.0`, `MAX_HOLD_HOURS = 12.0`
- `COOLDOWN_MIN = 90`, `MAX_TRADES_PER_DAY = 4`, `DAILY_LOSS_BREAKER_PCT = -4.0%`
- `BLOWOFF_GUARD_1H_PCT = 4.0`, `PULLBACK_MIN_PCT = 0.5`

**Exit logic:**
- Dynamic hard stop: `max(MIN_HARD_STOP_PCT, 0.5 * 6h_range)` on the negative side
- Take-profit cap: +6%
- Trailing TP: arms at +1.5%, gives back 0.7%
- Max-hold dead-bag: 12h + negative + broken 3h trend

### AI Overseer (`ai_overseer.py`)
Path: `traders/extreme/ai_overseer.py` (528 lines)

Runs hourly via cron (`run_ai_overseer.sh`). Cron: `0 * * * *`, no-agent mode.

**Flow:**
1. Gather state: portfolio value, open positions, recent trades, market snapshot (price, 6h range, 3h momentum for each of 12 pairs), current v2 config constants
2. Build prompt → call DeepSeek V4 Flash (`api.deepseek.com/v1`, model `deepseek-v4-flash`)
3. Parse JSON response
4. Apply parameter adjustments (bounded by `ADJUSTMENT_BOUNDS`)
5. Execute trade signals (market orders, max €30/trade, 1 position max for AI)
6. **NEW:** Write gates (`ai_overseer/ai_gate.json`)
7. Log script improvement ideas

**AI Prompt structure:**
- Current state (portfolio, positions, recent trades, market snapshot, config)
- Instructions: analysis, parameter adjustments (bounded), trade signals (max €30), gates (script_paused, consult_on_entry), script improvements
- Response must be valid JSON

**Safety limits:**
- `MAX_TRADE_SIZE_EUR = 30.0`
- `MAX_POSITIONS_AI = 1`
- `ADJUSTMENT_BOUNDS` for each param (e.g. VOL_FLOOR_PCT: 1.5-8.0, MIN_HARD_STOP_PCT: 1.0-5.0, etc.)
- Insufficient funds check before every trade

**Known issue:** `execute_trade` in ai_overseer.py queries `trading_state.quantity` for `SELL`, but `trading_state` table has NO `quantity` column. Falls back to `trade_log` BUY history. This works but is fragile.

### AI Gate System (NEW)
- `ai_overseer.py` → writes `ai_overseer/ai_gate.json`
- `load_ai_gates()` in v2 reads it
- `script_paused: true` → v2 skips ALL entries immediately
- `consult_on_entry: true` → v2 logs warning, continues scanning

### DB Layer (`db_prices.py`)
Path: `traders/extreme/db_prices.py` (405 lines)

Tables: `asset_prices`, `trading_state`, `notify_state`, `trade_log`.

**Known issues:**
- `trading_state` has NO `quantity` column (the AI Overseer SELL tries to read it)
- `ai_overseer.py` has its own inline DB functions instead of reusing `db_prices.py` — mostly duplicate code

### Cron Jobs (active)
- **AITrader 24/7 Crypto:** `*/5 * * * *` — runs `run_kraken_cycle.sh` (v2)
- **AITrader Kraken 24/7 Crypto:** `*/5 * * * *` — runs `run_kraken_cycle.sh` (legacy, same script? check)
- **AITrader AI Overseer:** `0 * * * *` — runs `run_ai_overseer.sh` (no-agent mode)
- **AITrader Crypto News Analyst:** `0 9,15,21 * * *`
- **AITrader Performance & Bug Monitor:** `0 */6 * * *`

---

## AI Overseer Logs (today)

Latest runs show:
- Last run at 14:00 EEST — had ~€3.47 available EUR (most locked in NEAR position)
- AI wanted to buy more NEAR but correctly rejected (insufficient funds)
- NEAR BUY @ 2.002 executed in first test run (€30, order OUIBXW-LRNOL-CIJJEJ)
- AI correctly adjusted MIN_HARD_STOP_PCT to 2.5, TRAIL_GIVEBACK_PCT to 0.7
- Gate file currently: `{"script_paused": false, "consult_on_entry": true, "reason": "Ask AI before entry"}`

---

## What Needs Review

### 1. AI Overseer (`ai_overseer.py`)
- **DB inconsistency:** `execute_trade()` for SELL queries `trading_state.quantity` but column doesn't exist. Fallback to `trade_log` works but is fragile. Should either add quantity to trading_state or fix the query.
- **Code duplication:** Has its own `get_db()`, `query_all()`, `query_one()`, `_log_trade()`, `_save_position()`, `_remove_position()` — many overlap with `db_prices.py`. Should refactor to use `db_prices.py`.
- **Prompt quality:** Is the prompt effective? Are we giving the AI enough/right data? Are the trade signals useful or noise?
- **Error handling:** What happens if DeepSeek API is down? JSON parsing fails? Trade fails mid-execution?
- **Gate file race condition:** The 5-min script reads the file every cycle. The Overseer writes once per hour. Is there a read/write conflict?
- **No logging of WHY an AI trade was skipped** (e.g., "insufficient funds" is logged but "not in market pool" is brief)

### 2. v2 Strategy (`execute_kraken_cycle_v2.py`)
- **Entry basis bug:** `entry_price` at buy (line 612-613) uses `current_price` from ticker, NOT the actual fill price from the order. Kraken market orders can slip. Should use actual fill from `create_market_buy_order` response.
- **Scoring function (line 571):** `score = t3 + 0.5 * rng + pullback - blowoff_penalty`. Is this optimal? Should it include volume? Market cap? Correlation?
- **Single-position risk:** `DEPLOY_FRACTION = 0.97` puts 97% in one coin. If that coin crashes, the account is devastated. Should there be a max position size relative to portfolio?
- **Dynamic stop formula (line 431):** `effective_stop = -max(MIN_HARD_STOP_PCT, 0.5 * rng_6h)`. Is 0.5 * range the right multiplier? Too tight? Too loose?
- **Max-hold exit (line 440-445):** Only exits if net-negative AND trend-broken. But what about a position that's +2% for 12h and going nowhere? Opportunity cost.
- **Heartbeat timing:** The `finalize()` closure captures `positions` and `new_state` references. Are there closure scoping bugs?
- **Gate timing:** AI gate check is AFTER the risk gates (step 3). Should it be BEFORE, so we don't waste API calls checking risk gates when AI paused?
- **No position sizing logic:** Just buys DEPLOY_FRACTION of cash. No Kelly, no volatility-adjusted sizing.

### 3. DB / Schema
- `trading_state` missing `quantity` column
- `trading_state` uses DELETE+INSERT approach instead of UPSERT — could lose state on crash between delete and insert
- No position-level PnL tracking in `trading_state`
- AI Overseer trades are logged with `signal_strength='AI_OVERSEER'` but v2 script uses `'PULLBACK_IN_UPTREND'` or `'EXIT'` — different conventions

### 4. General
- **Two parallel 5-min jobs?** Looking at cron, both "AITrader 24/7 Crypto" and "AITrader Kraken 24/7 Crypto" run */5. Are they both running v2? Race condition?
- **No backtest harness** for v2 with real data
- **No alerting** when bots crash or lose >X%
- **AI Overseer runs on DeepSeek V4 Flash** — it's fast/cheap but is it smart enough for trading decisions?
- **The consult_on_entry gate** currently has no enforcement — v2 just logs it but still enters
- **No token cost tracking** for AI Overseer calls

---

## Recent Git History
```
eba3e4b feat: AI gate system — Overseer sets gates, script obeys
fcfd78e fix: use explicit deepseek-v4-flash model
1cc5e99 chore: clarify deepseek-chat aliases to v4-flash
e80db01 feat: AI Overseer — hourly agent evaluates market, adjusts params, trades
a7691d8 track aux files: .ai reviews, db_prices, backtest scratch, requirements
7f037d9 kraken v2: add v1/v2 source tracking + Opus fixes (bounce gate, vol-aware stop, tighter trailing)
```
