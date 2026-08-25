# Crypto Strategy Edge Validation Implementation Plan

> **For Hermes:** Execute task-by-task with TDD and independent review. Do not promote a strategy to live automatically.

**Goal:** Determine whether any Kraken strategy has a repeatable positive net edge after realistic costs, while keeping `kraken-momentum` and `kraken-high-risk` in paper mode until explicit evidence-based promotion.

**Architecture:** Extend the shared backtest engine with chronological walk-forward evaluation. Training folds may select parameters; validation folds measure robustness; one final untouched holdout window is opened exactly once. Persist research artifacts only—never write live job modes or trading state from validation code.

**Tech Stack:** Python, pytest, PostgreSQL `asset_prices`, existing shared strategy signals, JSON/Markdown reports.

---

## Non-negotiable promotion gate

A strategy remains paper unless all conditions pass:

- At least 90 calendar days of sufficiently dense, sanity-checked price history.
- Kraken entry and exit fees included; slippage tested at base and stressed values.
- No lookahead: every decision sees only data available at that timestamp.
- Positive aggregate net expectancy across out-of-sample folds.
- Positive net result in a majority of folds and no single fold dominated by one trade.
- Minimum sample size of 30 closed trades across validation plus holdout.
- Profit factor above 1.15 after costs.
- Maximum drawdown below 10% in paper-sized simulation.
- Final untouched holdout is positive after stressed costs.
- Minimum 14 days of forward paper observation after candidate selection.
- Live promotion requires an explicit human action; validation code must not modify `cron_jobs.mode`.

If history or sample size is insufficient, the correct result is **INSUFFICIENT DATA**, not PASS.

### Task 1: Freeze and audit the research dataset

**Objective:** Produce deterministic data-quality evidence before optimization.

**Files:**
- Create: `scripts/audit_crypto_dataset.py`
- Create: `tests/test_crypto_dataset_audit.py`

**Steps:**
1. Write failing tests for duplicate timestamps, stale gaps, non-finite/non-positive prices, isolated outliers, per-symbol coverage, and deterministic UTC boundaries.
2. Run focused tests and confirm RED.
3. Implement a read-only audit that reports coverage and exclusions without changing production rows.
4. Run focused tests and confirm GREEN.
5. Run against a production DB snapshot/read-only connection and save a dated JSON artifact under `.ai/agent-reports/`.

**Acceptance:** The audit states exactly which symbols/date ranges are eligible. Fewer than 90 eligible days blocks later promotion.

### Task 2: Build chronological walk-forward folds

**Objective:** Prevent random-split leakage and parameter selection on future data.

**Files:**
- Create: `traders/research/walk_forward.py`
- Create: `tests/test_walk_forward.py`
- Modify: `scripts/backtest_strategy.py`

**Steps:**
1. Test deterministic expanding-window folds and a final untouched holdout.
2. Test that train, validation, and holdout timestamps never overlap.
3. Test a purge/embargo interval at least as long as the largest signal lookback.
4. Implement fold generation and expose it through a research-only CLI flag.
5. Verify identical inputs produce identical folds and output.

**Initial structure:** 60-day minimum training, 14-day validation folds, final 14-day holdout; increase windows when more history exists.

### Task 3: Model realistic execution costs

**Objective:** Reject edges that disappear under Kraken costs.

**Files:**
- Modify: `scripts/backtest_strategy.py`
- Modify: `tests/test_backtest_engine.py`

**Steps:**
1. Add tests for entry fee, exit fee, slippage on both sides, and partial exits.
2. Add stressed scenarios: base `0.26%` fee per side with `0.05%` slippage, and stress `0.35%` fee per side with `0.15%` slippage.
3. Assert all reported P&L is net of costs and capital conservation holds.
4. Keep the current zero-cost option only as a diagnostic—not a promotion metric.

### Task 4: Evaluate bounded strategy hypotheses

**Objective:** Test a small, predeclared parameter space rather than overfit arbitrary thresholds.

**Files:**
- Modify: `traders/strategies/momentum/signals.py`
- Create: `traders/research/candidates.py`
- Create: `tests/test_research_candidates.py`

**Candidate hypotheses:**
- Current momentum baseline.
- Maximum-extension guard to avoid late parabolic entries.
- Breakout-retest confirmation.
- Volume/liquidity confirmation only if trustworthy historical volume exists.
- BTC regime and direction filter already added in the safety release.

**Steps:**
1. Declare the parameter grid before opening the holdout.
2. Keep the grid intentionally small; do not optimize exits and entries simultaneously without enough trades.
3. Select on training only; report every validation result, including losing folds.
4. Reject candidates whose result depends on one coin or one trade.

### Task 5: Produce an evidence report and locked decision

**Objective:** Return PASS, FAIL, or INSUFFICIENT DATA with reproducible evidence.

**Files:**
- Create: `scripts/walk_forward_crypto.py`
- Create: `tests/test_walk_forward_cli.py`
- Output: `.ai/agent-reports/crypto_walk_forward_<date>.json`
- Output: `.ai/agent-reports/crypto_walk_forward_<date>.md`

**Report:** folds, parameters, trades, win rate, net expectancy, profit factor, drawdown, fee/slippage sensitivity, per-symbol concentration, holdout result, and gate failures.

**Safety:** The CLI must be read-only and contain no import/path capable of modifying `cron_jobs`, balances, orders, or trading state.

### Task 6: Forward paper observation

**Objective:** Confirm research/live parity against newly arriving data.

**Files:**
- Modify only if needed after evidence: existing paper telemetry and health report paths.

**Steps:**
1. Deploy the safety release with momentum/high-risk still paper.
2. Observe at least 14 days or 30 closed paper trades, whichever is later.
3. Compare paper decisions and fills against replayed decisions for the same timestamps.
4. Investigate every mismatch before promotion.
5. Require no data-integrity or duplicate-SELL incidents.

### Task 7: Controlled live canary—only after explicit approval

**Objective:** Limit capital risk if all research and paper gates pass.

**Steps:**
1. Present the complete validation and paper report to the user.
2. Require explicit approval to change the DB mode.
3. Start one strategy only at minimum allocation; never promote momentum and high-risk together.
4. Keep circuit breakers and monitor daily net results after fees.
5. Automatically return to paper on safety-trigger breach; do not auto-return to live.

## Verification commands

```bash
python3 -m pytest tests/ -q
python3 scripts/audit_crypto_dataset.py --exchange kraken
python3 scripts/walk_forward_crypto.py --exchange kraken --fees 0.26 --slippage 0.05
python3 scripts/walk_forward_crypto.py --exchange kraken --fees 0.35 --slippage 0.15
```

Expected outcome is not necessarily PASS. A truthful FAIL or INSUFFICIENT DATA is a successful validation result and keeps the strategy in paper mode.
