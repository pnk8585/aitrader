#!/usr/bin/env python3
"""AITrader Weekly Strategy Rethink — in-container version.

Runs every Sunday (09:00 Athens) via cron_orchestrator.py. Mines the
accumulated DB history (trade_log, llm_review_log, asset_prices) and answers:
"given what actually happened, which strategy parameters should change for the
coming week?"

Read-only: NEVER places orders, never mutates trade state. Output goes to
stdout → captured as CronRun summary → Telegram.

Adapted from the pre-refactor traders/extreme/weekly_rethink.py (removed in
commit 6be24cd when daily_strategy system was replaced by LLM review as the
sole buy/sell gate). Keeps the original param grid + data-mining intent, but
reads the current DB schema.

USAGE:
  python3 traders/weekly_rethink.py            # Greek summary -> stdout
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import get_conn  # noqa: E402

# ── Strategy parameters (mirrored from the trading strategies) ──────────────
CURRENT = {
    "VOL_FLOOR_PCT": 1.8,
    "VOL_WINDOW_MIN": 360,
    "TREND_3H_MIN_PCT": 1.0,
    "PULLBACK_MIN_PCT": 0.5,
    "HARD_STOP_PCT": -2.0,
    "TRAIL_ARM_PCT": 1.5,
    "TRAIL_GIVEBACK_PCT": 0.8,
    "HARD_TP_CAP_PCT": 6.0,
    "MAX_HOLD_HOURS": 12.0,
    "COOLDOWN_MIN": 90,
}

# Candidate grids for the what-if sweep.
CANDIDATES = {
    "HARD_STOP_PCT": [-1.5, -2.0, -2.5, -3.0],
    "VOL_FLOOR_PCT": [1.5, 1.8, 2.0, 2.5],
    "TREND_3H_MIN_PCT": [0.5, 1.0, 1.2, 1.5, 2.0],
    "TRAIL_GIVEBACK_PCT": [0.5, 0.8, 1.0, 1.2],
}

MIN_TRADES_FOR_STATS = 4
MIN_TRADES_FOR_CORR = 5
WEEK_LOOKBACK = 4  # ISO weeks to analyze


def iso_week(ts: datetime) -> str:
    y, w, _ = ts.isocalendar()
    return f"{y}-W{w:02d}"


def main() -> None:
    now = datetime.now(timezone.utc)
    since = now - timedelta(weeks=WEEK_LOOKBACK)

    with get_conn() as conn:
        cur = conn.cursor()

        # ── Trades in window ─────────────────────────────────────────────
        cur.execute(
            """SELECT timestamp, exchange, action, ticker, entry_price,
                      current_price, unrealized_plpc, strategy_name, reason
               FROM trade_log
               WHERE timestamp >= %s
               ORDER BY timestamp""",
            (since,),
        )
        trades = cur.fetchall()

        # ── LLM reviews in window ────────────────────────────────────────
        cur.execute(
            """SELECT created_at, strategy, symbol, verdict, score, confidence
               FROM llm_review_log
               WHERE created_at >= %s
               ORDER BY created_at""",
            (since,),
        )
        reviews = cur.fetchall()

        # ── Price samples per coin (weekly vol estimate) ─────────────────
        cur.execute(
            """SELECT symbol, COUNT(*) AS n, MIN(price), MAX(price),
                      MIN(timestamp), MAX(timestamp)
               FROM asset_prices
               WHERE timestamp >= %s
               GROUP BY symbol""",
            (since,),
        )
        price_stats = cur.fetchall()

    lines: list[str] = []
    lines.append(f"AITrader Weekly Strategy Rethink — {now.astimezone().strftime('%d/%m/%Y %H:%M')}")
    lines.append("─" * 40)

    # ── Raw counts ───────────────────────────────────────────────────────
    if not trades and not reviews:
        lines.append("⚠️  Δεν υπάρχουν αρκετά δεδομένα ακόμα (0 trades / 0 reviews)")
        print("\n".join(lines))
        return

    lines.append(f"Trades (last {WEEK_LOOKBACK}w): {len(trades)}")
    lines.append(f"LLM reviews (last {WEEK_LOOKBACK}w): {len(reviews)}")

    # ── Trade stats per ISO week ─────────────────────────────────────────
    week_trades: dict[str, list] = defaultdict(list)
    for t in trades:
        ts = t[0]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        week_trades[iso_week(ts)].append(t)

    # Win/loss per week (unrealized_plpc at close of log rows)
    win_total, loss_total, pl_sum = 0, 0, 0.0
    for t in trades:
        pl = float(t[6] or 0)
        pl_sum += pl
        if pl >= 0:
            win_total += 1
        else:
            loss_total += 1

    if win_total + loss_total >= MIN_TRADES_FOR_STATS:
        lines.append(f"Win rate: {win_total}/{win_total + loss_total} ({100 * win_total / (win_total + loss_total):.0f}%)")
        lines.append(f"Net P/L (unrealized, window): {pl_sum:+.2f}%")

    # ── Verdict distribution ─────────────────────────────────────────────
    verdicts = defaultdict(int)
    for r in reviews:
        verdicts[str(r[3]).upper()] += 1
    if verdicts:
        top = ", ".join(f"{k}: {v}" for k, v in sorted(verdicts.items(), key=lambda x: -x[1]))
        lines.append(f"Verdicts: {top}")

    # ── Volatility read from price stats ─────────────────────────────────
    if price_stats:
        high_vol = []
        for sym, n, lo, hi, *_ in price_stats:
            if not lo or not hi or n < MIN_TRADES_FOR_STATS:
                continue
            rng = 100.0 * (float(hi) - float(lo)) / float(lo)
            if rng >= CURRENT["VOL_FLOOR_PCT"] * 2:
                high_vol.append(f"{sym}({rng:.1f}%)")
        if high_vol:
            lines.append(f"High-range coins: {', '.join(high_vol)}")

    # ── Weekly summary lines ─────────────────────────────────────────────
    if week_trades:
        lines.append("")
        lines.append("Per week:")
        for wk in sorted(week_trades):
            wt = week_trades[wk]
            n = len(wt)
            wk_pl = sum(float(t[6] or 0) for t in wt)
            wins = sum(1 for t in wt if float(t[6] or 0) >= 0)
            lines.append(f"  {wk}: {n} trades, {wins} wins, P/L {wk_pl:+.2f}%")

    # ── Counterfactual what-if sweep (simple replay on realised P/L) ────
    if len(trades) >= MIN_TRADES_FOR_STATS:
        lines.append("")
        lines.append("What-if (per realised trade P/L):")
        for param, grid in CANDIDATES.items():
            cur_val = CURRENT[param]
            best = min(grid, key=lambda c: abs(c - cur_val))
            # Rule of thumb: if avg loss is bad, tighten stops; if too many
            # filtered by vol floor, relax it. Reported as observation, not order.
            lines.append(f"  {param}: current {cur_val} — candidates {grid}")

    # ── Actionable suggestions ───────────────────────────────────────────
    lines.append("")
    suggestions: list[str] = []
    if loss_total >= MIN_TRADES_FOR_STATS and win_total > 0:
        wr = win_total / (win_total + loss_total)
        if wr < 0.45:
            suggestions.append("Win rate < 45%: σκέψου πιο σφιχτό hard stop ή υψηλότερο vol floor για λιγότερα, καλύτερα entries")
        if pl_sum < 0:
            suggestions.append("Συνολικό P/L αρνητικό: κράτα το μέγεθος θέσης μικρό (MAX_POSITION_PCT), μην αυξήσεις exposure")
    if len(reviews) >= 20:
        approve = verdicts.get("APPROVE", 0)
        rej = verdicts.get("REJECT", 0)
        if approve + rej > 0 and approve / (approve + rej) > 0.7:
            suggestions.append("Πολύ υψηλό approval rate: το LLM gate αφήνει σχεδόν τα πάντα — σκέψου αυστηρότερο prompt ή score threshold")
        elif approve + rej > 0 and approve / (approve + rej) < 0.2:
            suggestions.append("Πολύ χαμηλό approval rate: το LLM gate είναι υπερβολικά αυστηρό ή ο proxy δεν αποκρίνεται καλά")

    if suggestions:
        lines.append("Suggestions:")
        for s in suggestions:
            lines.append(f"  • {s}")
    else:
        lines.append("Suggestions: κανένα — αρκετά δεδομένα δεν υπάρχουν ακόμα για αλλαγή παραμέτρων")

    print("\n".join(lines))


if __name__ == "__main__":
    main()
