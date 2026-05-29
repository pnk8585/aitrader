# Crypto-Momentum Strategy — Backtest Findings

Date: 2026-05-29. Tool: `traders/extreme/backtest.py` (stdlib + Kraken public OHLC, no deps).

## Setup
- Rules = current live wired values (TTP peak 3.0/giveback 1.0, PLOCK 5.0/3.0, stop -3.5,
  breakeven fee-floor 0.6, time-stop 1h, entry intraday change >= 2.0%).
- Fees: 0.26%/side (Kraken taker). Per-symbol expectancy, 15m candles, latest ~7.5 days, 12 pairs.

## Result — NEGATIVE EDGE
| Metric | Value |
|---|---|
| Trades | 142 |
| Win rate | **18.3%** (26W/116L) |
| Avg win | +1.41% |
| Avg loss | -1.17% |
| Expectancy/trade | **-0.695%** (net fees) |
| Break-even WR required | 45.3% |
| Compounded (50% sizing) | **-39.2%** over 7.5d |

**Every single pair lost.** Worst: RENDER -35%, NEAR -21%, DOT -14% (high-churn alts).

## Why it loses
- Entry **chases** momentum (buys AFTER +2% intraday). In 15m crypto chop, most fade back.
- TTP needs +3% peak to even arm → rarely reached → most exits are breakeven-floor (~net 0),
  time-stop (small loss), or stop (-3.5%). Avg win too small vs loss frequency.
- High-beta alts (RENDER/NEAR/SUI) whipsaw → many -fee round trips.

## Caveats
- 15m granularity: time-stop (1h=4 bars) coarse; intrabar exit ordering simplified (stop first).
- Only 7.5 days (recent regime). Per-symbol; ignores live position-limit/best-signal selection.
- Signal is overwhelming though (18% WR, all pairs negative) — unlikely to flip with finer data.

## Recommendation
Do NOT run live as-is. Options:
1. Parameter sweep (entry threshold, lower TTP arm, tighter stop) to find any positive config.
2. Flip to mean-reversion (buy dips, not breakouts) — opposite of current logic.
3. Reduce universe to liquid majors only (BTC/ETH/SOL), cut alt churn.
