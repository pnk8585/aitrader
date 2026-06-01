# Weekly Strategy Rethink — `weekly_rethink.py`

Read-only Sunday analysis of accumulated `asset_prices` + `trade_log` data.
Mines patterns, replays counterfactual parameters, and **suggests** (never
applies) changes for the coming week's AITrader v2 Kraken strategy.

> ⚠️ It never trades, never mutates state, never edits the live strategy.
> Its only writes are two report files (below).

---

## What it does

Source of truth for the *current* parameters is
`execute_kraken_cycle_v2.py` (top of file). They are **mirrored** into the
`CURRENT` dict in `weekly_rethink.py` — keep the two in sync when you tune v2.
(The module is deliberately not imported: importing it builds a CCXT client and
`sys.exit()`s without Kraken creds, which would break a headless cron.)

### Per coin, per ISO week
- mean hi-lo **range %** and **directional bias** (close vs open %)
- **win/loss rate** and net PnL (after the 0.52% round-trip fee)
- **avg hold time** for winners vs losers

### Across the whole period
| Pattern | How it's detected |
|---|---|
| Volatility compression / expansion | week-over-week mean range % |
| Trend persistence | does a coin's +bias week beget another +bias week |
| Coin rotation | who enters / leaves the `VOL_FLOOR_PCT` zone vs last week |
| Time-of-day clusters | mean net return bucketed by entry UTC hour |
| Fee efficiency | % of round-trips whose **gross** return cleared the fee |
| Parameter sensitivity | the what-if sweep below |

### Parameter what-if (counterfactual replay)
For each candidate threshold we replay against the **real price path** in
`asset_prices`:
- **HARD_STOP_PCT** `[-1.5, -2.0, -2.5, -3.0]` — how many losers flip to winners
- **TRAIL_GIVEBACK_PCT** `[0.5, 0.8, 1.0, 1.2]`
- **VOL_FLOOR_PCT** `[1.5, 1.8, 2.0, 2.5]` — how many trades get filtered, net of filtered
- **TREND_3H_MIN_PCT** `[0.5, 1.0, 1.2, 1.5, 2.0]`

Exit params (stop, giveback) use full price-path replay of the v2 exit ladder.
Entry filters (vol floor, trend) recompute the entry-time condition from
`asset_prices` and tally PnL of filtered-out vs retained trades.

**Indeterminate trades are never silently dropped** — any trip without price-path
coverage (e.g. trades that predate price logging) is counted in
`indeterminate_trips` and excluded from the verdict.

---

## Outputs

### stdout (what cron forwards to Telegram) — Greek, concise
```
python3 weekly_rethink.py                # default: --format telegram
```
```
📊 Εβδομαδιαία Ανασκόπηση AITrader v2 (31/5/2026)
📈 Απόδοση: Trades / Wins / Net / fee-loss%
🔥 Hot coins: top range + new vol-zone entrants
⚙️ Προτεινόμενες αλλαγές: only when the replay justifies one
🔍 Patterns: winner/loser entry hours, vol trend, corr
```

### Files (always written, regardless of `--format`)
| Path | Content |
|---|---|
| `~/.hermes/cron/output/strategy_rethink_recommendations.json` | machine-readable; current vs suggested per param, confidence, evidence. `"apply": false` |
| `~/.hermes/cron/output/strategy_rethink_report.md` | full markdown: per-week/coin tables, patterns, what-if tables, verdicts |

The recommendations JSON is the **future adaptive hook**: a later version of
`execute_kraken_cycle_v2.py` *could* read it to self-tune. It does **not** today
— `"apply": false` and the `note` field make that explicit.

---

## CLI

```
python3 weekly_rethink.py                 # Greek Telegram summary -> stdout
python3 weekly_rethink.py --format markdown
python3 weekly_rethink.py --format json
python3 weekly_rethink.py --format all     # telegram + markdown + json
python3 weekly_rethink.py --weeks 4        # only the last 4 ISO weeks
python3 weekly_rethink.py --no-write       # don't touch the report files
```

Idempotent and safe to run manually any time.

---

## Small-dataset behavior

`asset_prices` currently holds only ~9h of data. Guards:
- `MIN_PRICE_SAMPLES = 12` — fewer samples per coin/window ⇒ that range/trend is `None`
- `MIN_TRADES_FOR_STATS = 4` — fewer round-trips ⇒ `status: "insufficient_data"`
- `MIN_TRADES_FOR_CORR = 5` — correlation reported only above this

On a thin dataset the Telegram block becomes:
```
⚠️ Δεν υπάρχουν αρκετά δεδομένα ακόμα.
```
and the markdown adds an explicit "ενδεικτικοί αριθμοί" warning. As history
accumulates the same code path produces trustworthy signal — no changes needed.

---

## Cron setup

Schedule it for **Sundays 09:00 Europe/Athens**. The script is standalone
(no skills needed) and read-only.

```
cronjob(
  action="create",
  schedule="0 9 * * 0",
  timezone="Europe/Athens",
  prompt="Run the weekly rethink analysis: python3 PROJECT_ROOT/traders/extreme/weekly_rethink.py and send the stdout to Telegram verbatim.",
  skills=[]
)
```

- `0 9 * * 0` = every Sunday at 09:00.
- Timezone `Europe/Athens`: in summer (EEST, UTC+3) that is **06:00 UTC**;
  in winter (EET, UTC+2) it is **07:00 UTC**. Let the cron's `timezone` field
  handle DST — don't hardcode a UTC hour.
- It only analyzes and reports; nothing to roll back.

Manual smoke test before relying on the schedule:
```
python3 PROJECT_ROOT/traders/extreme/weekly_rethink.py --format all
```

---

## Interpreting the report

- **Net %** is already after fees (`gross - 0.52`). A coin with a positive
  win-rate but negative net is being eaten by fees — that's the v1 failure mode.
- **`corr(entry momentum, return)` near 0** (e.g. +0.08) means the entry signal
  is noise, exactly the v1 postmortem finding. The script emits a
  `_signal_warning` when `|corr| < 0.1`.
- **A what-if row only changes a recommendation when** the candidate has real
  price-path coverage AND improves net PnL (exit params) or filters net-negative
  trades (entry filters). Otherwise it reports `keep`.
- **Confidence** scales with total round-trips (`low < 4 ≤ medium < 12 ≤ high`).
  Cross-check against `covered_trips` / `indeterminate_trips` in the evidence —
  a "high" verdict built on few covered trips is still thin.

> Recommendations are advice, not auto-applied. Review, then edit the params at
> the top of `execute_kraken_cycle_v2.py` by hand if you agree.
