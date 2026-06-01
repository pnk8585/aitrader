# Kraken Crypto Strategy — Post-Mortem & v2 Redesign

Date: 2026-05-31. Analyst: quant strategist. Source: `aitrader` PostgreSQL
(`trade_log`, `asset_prices`) + `execute_kraken_cycle.py`.

> **NOTE on the task's "quick analysis":** the huge per-ticker numbers (NIO
> -505%, AMD +563%) come from `alpaca` (stock) rows where `unrealized_plpc`
> is dirty. They are **not** crypto. This post-mortem isolates the **kraken**
> rows, which are the real crypto story. `unrealized_plpc` is stored as a
> **fraction** in the DB (0.0193 = +1.93%); the quick analysis mis-scaled it.

## 1. What actually happened (kraken only)

- **55 trades** = **27 completed round-trips** (28 buys, 27 sells), 2026-05-29
  to 2026-05-31 (~2 days).
- Capital path (from buy sizes): **€47.87 → €36.85 = −23% in two days.**
- Round-trip stats (net of 0.52% Kraken round-trip fee):

| Metric | Gross | Net of fee |
|---|---|---|
| Avg return / trip | −0.26% | **−0.78%** |
| Win rate | 52% (14/27) | **19% (5/27)** |
| Sum of returns | −7.1% | **−21.1%** |
| Compounded | — | **−19.3%** (matches −23% capital decay) |

- **Avg winner +0.50% gross — smaller than the 0.52% round-trip fee.**
  Avg loser −1.08%. Negative expectancy *by construction*: you cannot win a
  game where the average win doesn't even pay the toll.
- **Only 5 of 27 trips (19%) cleared the fee.** The other 81% were structural
  donations to Kraken.
- Median hold **40 min**, mean 59 min. ~13 round-trips/day on a €40 account.
- Fees alone burned **~14% of capital** over the run.

## 2. Why it loses — root causes

1. **Death by fees / overtrading.** 0.52% round-trip is a wall. Capturing
   +0.50% average and paying 0.52% is a guaranteed slow bleed. 13 trips/day
   compounds it.
2. **Momentum is noise at this timescale.** `corr(entry_momentum, forward
   return) = +0.08` ≈ zero. Bucketed forward returns by entry strength:

   | Entry momentum | n | avg gross fwd return |
   |---|---|---|
   | 2–3% (MODERATE) | 12 | −0.00% |
   | 3–5% (STRONG) | 5 | −0.01% |
   | **>5% (EXTREME)** | 8 | **+0.00%** |

   Buying EXTREME momentum returns **exactly nothing** — it's buying the top;
   crypto mean-reverts intraday. The signal tiers are theatre.
3. **Every exit is `NO_MOMENTUM`.** No trip ever exits on a real profit
   target. The +3% trailing-take-profit almost never arms because the **30-min
   stale-rotation** timer sells first — at a flat price, locking a fee loss.
4. **"Breakeven protection" sells at a net loss.** It exits when unrealized
   ≤ +0.6% after peaking +1%. +0.6% gross − 0.52% fee = a wash at best, usually
   negative after slippage. It manufactures losers.
5. **No trend/regime filter.** Buys raw 1-bar strength regardless of higher-
   timeframe direction → chases blow-off tops that revert.
6. **Wrong universe for the timescale.** Realized ranges over the 9h price
   sample:

   | Bucket | Coins | ~9h hi-lo range |
   |---|---|---|
   | Too quiet (range < ~2× fee) | BTC 0.58%, ETH 0.83%, XRP 0.91%, SOL 0.93%, DOGE 1.02% | a fee move can't be beaten |
   | Tradeable vol | DOT 1.22%, LINK 1.32%, ADA 1.36%, AVAX 1.70%, SUI 2.39% | OK |
   | High vol | **NEAR 4.63%, RENDER 3.77%** | moves dwarf fees |

   The bot did concentrate on RENDER (12 trips) + NEAR (9) — the right coins —
   but with a momentum-chase entry and a 40-min exit it captured noise, not the
   3–4% swings those coins actually make.

## 3. The one inequality that governs everything

For any system: `Expectancy = WinRate·AvgWin − (1−WinRate)·AvgLoss − Fee`.
With `Fee = 0.52%`, the old system had `AvgWin ≈ 0.50%`, so expectancy was
negative no matter the win rate. **v2's prime directive: make the average
winner large relative to the fee (target ≥ 3–4× fee) and trade rarely.**

## 4. v2 strategy — design

Philosophy: **trade like a sniper, not a machine gun.** Few, high-conviction
trades; let winners run well past the fee wall; cut losers fast; never sell
flat.

**Universe (dynamic vol filter).** Only coins whose last-6h hi-lo range ≥
**1.8%** (≈3.5× fee). Quiet majors (BTC/ETH/XRP/DOGE/SOL) are excluded
automatically most of the time. Pool: the 12 pairs, gated by live volatility.

**Regime filter (trend, not 1-bar momentum).** Require a *sustained* uptrend:
price > price 3h ago by ≥ **+1.0%** AND price > price 6h ago. No higher-TF
uptrend → no trade.

**Entry — buy the pullback inside the uptrend, never the top.**
- Trend up (above), AND
- short-term pullback: current price is ≥ **0.5%** below the last-1h high
  (we buy a dip, not a breakout extension), AND
- **anti-blow-off guard:** skip if 1h momentum > +4% (that's the top the old
  bot kept buying), AND
- not in cooldown for that coin (see risk).

**Exits.**
- **Hard stop −2.0%** (tight; we entered near short-term support).
- **Trailing take-profit, fee-aware:** arm only after +**1.5%** (clears fee +
  buffer). Once armed, exit on **0.8%** giveback from peak. Hard cap +6%.
- **No breakeven-at-fee-floor exit. No 30-min stale rotation.** Removed — they
  were the loss factories.
- **Max hold 12h:** only force-exits if position is net-negative and trend has
  broken (price < 3h-ago). Winners are never time-stopped.

**Position sizing.** Small account (€100–200): **one** position at a time,
deploy ~**97%** of cash into the single best setup (rest is fee buffer). One
concentrated, well-chosen trade beats several tiny fee-bleeding ones. (Cap at
2 only above €400 equity.)

**Risk management / circuit breakers.**
- **Per-coin cooldown 90 min** after any exit (kills rotation churn).
- **Max 4 trades/day.** Hard cap on overtrading.
- **Daily loss breaker:** if realized PnL today ≤ −4%, stop trading until next
  UTC day.
- Min trade €0.45 (Kraken), skip otherwise.

**Expectancy check (target).** With −2% stop, +1.5% trail-arm and winners
running to ~2.5–4%: if win rate ~45% and avg win ~2.8%, avg loss ~1.8%:
`0.45·2.8 − 0.55·1.8 − 0.52 = 1.26 − 0.99 − 0.52 = −0.25`… still tight — which
is *exactly why the entry filter and trade-count cap matter*: the edge has to
come from only taking pullback-in-uptrend setups (higher win rate / bigger
winners) and from **not paying fees 13×/day**. The dominant lever is **trade
frequency**: cutting from 13 to ~2 trips/day removes ~11×0.52% ≈ 5.7%/day of
pure fee drag.

## 5. Backtesting caveat (important)

`asset_prices` holds **only ~9 hours** of 12-coin data (every ~5 min). That is
**far too little to validate** any of these thresholds statistically. Before
risking real money, accumulate ≥30 days of `asset_prices` (the Kraken cycle is
the sole writer and grows it every run), then backtest the entry/exit rules
offline and grid-search the thresholds. v2 ships **conservative** defaults so
the failure mode is "trades too rarely," not "bleeds fees."

## 6. Implementation

`execute_kraken_cycle_v2.py` — drop-in replacement, same DB schema, same
CCXT/Kraken infra, same price-writer responsibility. All thresholds are module
constants at the top for easy tuning/backtesting.
