# AITrader — Deep Code Review (2026-06-01)

Reviewer: Claude (Opus). Scope: all `traders/extreme/*.py`, `.env`, scheduling, strategy docs.

## Headline findings

1. **Nothing schedules the cycle.** Empty crontab, no systemd timer, no `.sh`/`.service`,
   no loop in v2 (`run_cycle` runs once & exits). `logs/` has one file from 2026-04-02.
   `TRADING_CYCLE_SECONDS=60` in `.env` is read by no code. This — not the scan bug
   (`31663bf`) — is why trade logs are empty.
2. **v2 cannot buy without pre-existing price history.** Entry requires 3h+6h trend from
   `asset_prices` (`execute_kraken_cycle_v2.py:490-492`); the cycle is the *sole* price
   writer. No persistent scheduler → no 6h continuous history → `get_momentum_over` returns
   `None` → all candidates filtered → zero trades.
3. **No validated edge.** Research bottoms out at −0.123% expectancy
   (`backtest_alt_signals7.py`, NEAR/RENDER+pullback, 47.4% WR). Live-rules backtest:
   −0.695%/trade, 18.3% WR. Postmortem's own optimistic v2 math = −0.25%. Expected-negative.

## Bugs

### execute_kraken_cycle_v2.py (live)
- `:549` entry_price = pre-trade ticker `last`, not actual fill → wrong basis for all P&L,
  stop, trail, peak. Use `res.get('average')` from the order result.
- `:555` position dict written with only `symbol`+`current_price` (fragile).
- `:411,:541` `load_markets()` per order (redundant network). Load once at startup.
- No concurrency lock → overlapping cycles can double-order. Add pidfile/flock.
- `:530` `min(cash*0.97, cash)` is a no-op.
- `:217-238` `realized_pnl_today_pct` sums P&L computed off wrong entry basis → loss
  breaker on bad data.

### execute_cycle.py (Alpaca)
- `:20` base URL hardcoded LIVE; paper fallback removed (`544a019`). Real money.
- `:132-136` failure `return` without finalize()/close_connection → DB conn leak.
- `:229-240` stale-rotation (age≥0.5h & <1%) = postmortem's #1 loss factory, still wired.
- `:218` breakeven protection sells at net ≤0 — manufactures losers.

### Dead / duplicate
- `execute_kraken_cycle.py` is byte-identical to `execute_kraken_cycle_v1.py` (both losing v1).
- 13 backtest scratch files (`backtest_alt_signals*.py`, `_v2/_meanrev/_binance/_inverse/`
  `_path_analysis/_quick`). Findings already captured in `.ai/`. Move to `research/` or delete.

## Risk / sizing
- v2 `DEPLOY_FRACTION=0.97` + 1 position = 97% in one illiquid alt. `MAX_SINGLE_TICKER_PCT=0.80`
  never read by v2.
- `execute_cycle.py:490` small-account rule deploys 100% (overrides `MAX_POSITION_PCT=0.50`).
- Kelly with negative/unproven edge = 0. Size belongs at paper until a config backtests
  clearly positive *with realistic slippage*.
- v2 circuit breakers (cooldown 90m, 4 trades/day, −4% daily breaker) are good — keep.

## Entry / exit
- Momentum chase fails: corr(entry,fwd)=+0.08; EXTREME bucket fwd return +0.00%.
- v2 pullback-in-uptrend is the right *direction* but never crossed zero in research.
- v2 exit *shape* is sound (stop −2, trail arm +1.5/giveback 0.8, cap +6, dead-bag time-stop).
- Real exit cost = market orders into thin EUR books; slippage unmodeled.

## What would make it profitable (by leverage)
1. **Maker/post-only limit orders** — Kraken maker 0.16% vs taker 0.26%, kills slippage.
   Round-trip wall 0.52%→0.32%. Biggest single lever.
2. **Add a real scheduler** (systemd timer/cron, continuous) so `asset_prices` accumulates.
3. **Liquid majors for exits**; alts only with limit entries.
4. **Model slippage+spread in backtest, re-validate v2's actual rules** (currently untested;
   backtest tested v1 rules). Add pre-buy spread guard.
5. **≥30 days data before grid-search** (postmortem §5: 9h is statistically worthless).
6. **Until clearly-positive net backtest: paper only, size = 0.**

## Security
- `.env` holds plaintext LIVE Alpaca + Kraken keys + DB password. Gitignored (good) but on a
  box with no order lock / kill switch. ACTION: rotate DB password; confirm exchange keys are
  trade-only (no withdrawal scope).
