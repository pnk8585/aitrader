# 7-Day Live-Data Review Plan

Created: 2026-05-29 · **Review date: 2026-06-05**

## Why
Backtest on 7.5d historical data showed negative edge (see [backtest-findings.md](backtest-findings.md)),
but that is too short a window and the live Kraken bot had **zero** accumulated trade logs
(`kraken_trades-*.jsonl` empty — the scan-indent bug had blocked all trades until the 2026-05-29 fix).
Decision: leave strategy + tuning constants AS-IS, let it run 7 days, then judge on REAL fills.

## What is running (frozen for this window)
- `execute_cycle.py` (Alpaca, USD) + `execute_kraken_cycle.py` (Kraken, EUR).
- Ride-the-wave exits: TTP peak 3.0 / giveback 1.0, PLOCK 5.0 / 3.0, stop -3.5,
  breakeven fee-floor (0.5 USD / 0.6 EUR), time-stop 1h, entry intraday >= 2.0%.
- Do NOT change these constants before 2026-06-05 — we need a clean sample.

## Checklist for 2026-06-05
1. **Confirm it traded**: `ls -la logs/kraken_trades-*.jsonl logs/trades-*.jsonl` — expect non-empty
   files dated 2026-05-29 .. 06-05. If still empty → bot not firing (check cron/systemd, creds, scan).
2. **Count fills**: number of BUY and matching SELL actions per bot.
3. **Realized P&L from real fills** (ground truth, beats simulation):
   - Pair each BUY with its closing SELL by ticker (FIFO).
   - net% = (sell_price - buy_price)/buy_price*100 - round_trip_fee.
   - Report: win rate, avg win, avg loss, expectancy/trade, total/compounded return.
4. **Compare to backtest** (18.3% WR, -0.70%/trade expectancy). Did live confirm or contradict?
5. **Exit-reason breakdown**: how often each exit fired (TTP / PLOCK / stop / breakeven / time-stop /
   rotation). If time-stop + breakeven dominate and TTP/PLOCK rarely fire → momentum-chase confirmed weak.
6. **Per-pair**: which pairs bled (expect alt churn: RENDER/NEAR/SUI/DOT).

## Decision gate (2026-06-05)
- **Expectancy > 0 and WR >= break-even** → keep running, maybe widen universe.
- **Expectancy <= 0** → stop live, then choose:
  1. Parameter sweep (entry threshold, lower TTP arm, tighter stop).
  2. Flip to mean-reversion (buy dips, not breakouts).
  3. Majors-only (BTC/ETH/SOL), cut alt churn.

## Helper
`traders/extreme/backtest.py` can be adapted into a log-based analyzer for step 3
(swap the OHLC fetch for reading the realized BUY/SELL fills from the jsonl logs).
