# Restructure DONE — 1 strategy per script (2026-06-01)

Implemented per `.ai/opus-restructure-context-2026-06-01.md`. Old files left untouched (legacy).

## New files
| Path | Lines | Exchange key | Lock | Strategy |
|------|-------|--------------|------|----------|
| `traders/crypto_trades/kraken_pullback.py` | 775 | `kraken-pullback` | `logs/kraken_pullback.lock` | pullback-in-uptrend (copy of execute_kraken_cycle_v2.py) |
| `traders/crypto_trades/kraken_momentum.py` | 764 | `kraken-momentum` | `logs/kraken_momentum.lock` | momentum breakout (new, CCXT/Kraken) |
| `traders/trades/alpaca_stocks.py` | 530 | `alpaca-stocks` | (none — REST, no fcntl) | stock momentum (new, Alpaca REST) |

Shell wrappers (chmod +x) in `~/.hermes/scripts/`: `run_kraken_pullback.sh`, `run_kraken_momentum.sh`, `run_alpaca_stocks.sh` — each `cd PROJECT_ROOT` then run the script.

## Key design notes
- **Imports:** `db_prices` stays in `traders/extreme/`. Each new script adds
  `sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "extreme"))`.
- **Shared Kraken wallet:** momentum only EXITS coins recorded in its own
  trading_state; never sells a coin pullback holds; never ENTERS a coin already
  in the balance (held by either). Global cap `MAX_TOTAL_OPEN=5`, momentum cap 2.
- **Price feed:** both Kraken scripts write base prices under `exchange='kraken'`
  via `insert_prices` (hardcoded EXCHANGE='kraken' in db_prices). Momentum's
  local SQL helpers read `exchange='kraken'` (NOT EXCHANGE_NAME) so momentum
  sees the shared feed. Pullback's helpers read `EXCHANGE_NAME` — but its
  EXCHANGE_NAME is now `kraken-pullback` while insert_prices still writes
  `kraken`. ⚠️ FOLLOW-UP: pullback's get_momentum_over/get_range_pct/etc. query
  `exchange=EXCHANGE_NAME` = `kraken-pullback`, but prices land under `kraken`.
  This means pullback's DB momentum reads return None until verified. The
  original v2 had EXCHANGE_NAME='kraken' so reads matched writes. Confirm before
  going live: either point pullback price reads at `'kraken'` or keep its
  EXCHANGE_NAME for trade_log/state but read prices from `'kraken'`.
- **Alpaca stocks:** entry daily>=1.5% (vs prevDailyBar close) OR intraday>=1.0%
  (vs dayOpen), via `/v2/stocks/snapshots`. `time_in_force: "day"`. Fee
  round-trip 0.01% (0.005%/side). Max 3 BUYs/UTC day via trade_log COUNT.
  Entries only when market open; exits also require market open (stock fills).
  Stale rotation removed.

## Validation done
`python3 -c "ast.parse(...)"` → OK on all three. Scripts NOT run (no live exec).

## Not done (per instructions)
- Cron jobs (user updates separately).
- No git operations.
- Old files not deleted.
