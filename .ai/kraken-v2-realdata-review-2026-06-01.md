# Kraken v2 real-data review — 2026-06-01

## Headline: the premise was wrong, again

The -9.91% (43.61€ → 34.72€/33.75€) over 30/5→1/6 is **almost entirely v1**, not v2.

### Proof
- `md5sum`: `execute_kraken_cycle.py` == `execute_kraken_cycle_v1.py` (byte-identical, `9962b54…`).
  `execute_kraken_cycle_v2.py` is a *different* file (`6be148c…`) and is **not** the scheduled name.
- No cron in `pank`'s crontab, no systemd timer, logs stale (Apr 2). Prices still write every 5 min
  (12 cycles/hr, ongoing) → scheduler exists but is invisible (root cron, no sudo). Cannot confirm
  which file it calls — but the file literally named `execute_kraken_cycle.py` IS v1 right now.
- trade_log exit reasons split cleanly by code version:
  - v1 reasons (Stale Crypto Rotation, Breakeven protection, Held >1h stale, MOMENTUM buys):
    every trade 05-30 01:20 → 05-31 10:05.
  - v2 reasons (PULLBACK_IN_UPTREND buys, "Trailing TP … net …", "Hard stop … <= -2.0%"):
    only 06-01 00:00 → 04:25, then nothing for 4h.

### Attribution (SELLs since 30/5)
| Era | round-trips | gross sum | net after 0.52% fee |
|-----|-------------|-----------|---------------------|
| v1 (30/5–31/5) | 20 | -6.44% | **-16.84%** |
| v2-pure (1/6)  | 3  | -3.47% | -5.03% |

User's "exit breakdown" is misattributed: Stale Rotation ×11/12, Breakeven ×3, Held>1h ×4 are **all v1**.
v2 has no rotation/breakeven/stale code at all. Pure v2 = 3 trades: 1 Trailing-TP win (+0.70% gross /
+0.18% net), 2 immediate hard stops (-2.09%, -2.08%).

## Real v2 problems (from the 3 live trades)
1. **Entry catches falling knives.** Pullback (v2 L500-506) = only "price ≥0.5% below 1h high".
   No confirmation the dip bottomed/bounced. 2 of 3 entries kept falling straight to the -2% stop in
   <90 min. The "pullback in uptrend" is indistinguishable from "start of a reversal" without a bounce gate.
2. **Stop band sits inside the noise band.** `VOL_FLOOR_PCT=1.8` (L71) admits coins whose 6h range is
   1.8%, while `HARD_STOP_PCT=-2.0` (L82). A 1.8–2% intrabar wiggle = random stop-out. Stop must be a
   function of volatility, or vol floor must be >> stop.
3. **Trailing barely clears fees.** `TRAIL_ARM 1.5% / GIVEBACK 0.8%` (L83-84): the one winner armed at
   +1.56%, gave back to +0.70%, netted +0.18%. Giveback eats >half the arm.

## Concrete fixes (execute_kraken_cycle_v2.py)
- **#0 DEPLOYMENT (blocking):** confirm what cron calls. If it calls `execute_kraken_cycle.py`, copy v2
  over it (or repoint the schedule). Until then every v2 edit is dead code. Verify with
  `grep PULLBACK execute_kraken_cycle.py` returning a hit.
- **Entry bounce gate (L500-506):** require the dip to be turning. Add: latest price > price 1 cycle
  (~5min) ago, OR price > rolling 15-min low * 1.001. Reject if still making lower lows.
- **Vol-aware stop (L82, L391):** `stop = -max(2.0, 0.5 * range6h)`; and raise `VOL_FLOOR_PCT` to ≥3.0 so
  the -2% stop is outside the noise.
- **Tighten giveback after arm (L84):** `TRAIL_GIVEBACK_PCT 0.5`, arm at `TRAIL_ARM 1.2`, so a +1.5% peak
  locks ~+1.0% gross (~+0.5% net) instead of +0.18%.
- Alpaca `execute_cycle.py` is the same code family (momentum + rotation = v1 pattern); same fixes apply
  but it was out of scope for the live loss here.
