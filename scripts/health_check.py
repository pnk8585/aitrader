#!/usr/bin/env python3
"""Nightly health check for AITrader — container cron job.

Runs daily inside the container (JOB_REGISTRY "health-check", 86400s).
Queries the trading DB and reports:

  1. Cron health   — per-job run counts, errors, staleness (did crons run?)
  2. Missed ticks  — 5m jobs that skipped a slot (opportunities lost)
  3. LLM gate      — REJECT/APPROVE/HOLD ratios, silent days (prompt problems)
  4. Price feed    — asset_prices freshness per exchange
  5. Trades        — today's trades vs approvals (did approvals materialize?)
  6. Modes         — enabled/mode per job (DB is truth, not registry)

Severity: OK → brief; WARN/CRIT → detailed report + fix suggestions.

Output goes to stdout → captured as CronRun summary. If severity is
WARN/CRIT, a Telegram alert is sent via app.notify (non-trade job → always notify).
"""

from __future__ import annotations

import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import get_conn

# Expected cadence per job (interval seconds from JOB_REGISTRY)
EXPECTED_INTERVAL: dict[str, int] = {
    "kraken-pullback": 300,
    "kraken-grid": 300,
    "kraken-momentum": 300,
    "kraken-high-risk": 300,
    "alpaca-stocks": 300,
    "position-monitor": 7200,
    "end-of-day-review": 86400,
    "db-cleanup": 86400,
    "weekly-rethink": 7 * 86400,
}

# 5-minute trading jobs — gaps here mean lost opportunities
FIVE_MIN_JOBS = {"kraken-pullback", "kraken-grid", "kraken-momentum",
                 "kraken-high-risk", "alpaca-stocks"}

# How many intervals of silence before we flag a job as stale
STALE_FACTOR = 3
# Max acceptable gap for 5m jobs (seconds) before flagging a missed tick
MAX_GAP_SECONDS = 7 * 60  # 7 minutes (observed baseline ~5.5min due to shared scheduler)


# Jobs that are NOT expected to run every day (exclude from daily checks):
# both run weekly (Sunday) per app/cron_orchestrator.py, so having no runs
# in a given 24h window is expected, not an issue.
DAILY_EXCLUDED = {"weekly-rethink", "llm-review-report"}

# Alpaca defers when US market closed — skip staleness/gap checks for it
# (the scheduler re-arms it at next market open)
MARKET_WINDOWED = {"alpaca-stocks"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def missing_run_issues(db_jobs: dict[str, dict], per_job: dict[str, dict]) -> list[str]:
    """Enabled jobs with 0 runs in the window — weekly (DAILY_EXCLUDED) and
    market-windowed jobs legitimately have no daily runs, so they are skipped."""
    issues: list[str] = []
    for job, meta in sorted(db_jobs.items()):
        if job in DAILY_EXCLUDED or job in MARKET_WINDOWED:
            continue
        if job not in per_job and meta["enabled"]:
            issues.append(f"⚠️ {job}: enabled in DB but 0 cron_runs in 24h")
    return issues


def fix_suggestions(issues: list[str], warns: list[str]) -> list[str]:
    """Human-readable fix suggestions for the issues/warnings found."""
    suggestions: list[str] = []
    joined = " ".join(issues).lower()
    all_text = joined + " " + " ".join(warns).lower()
    if "0 runs in 24h" in joined or "enabled in db but 0" in joined:
        suggestions.append("Check `docker logs aitrader --tail 100` for scheduler errors; verify the job script still exists; `docker exec aitrader python -m app.cron_orchestrator run <job>` to test manually.")
    if "stale" in joined:
        suggestions.append("Scheduler may be blocked on a long subprocess — check `/state/logs/scheduler.log` for timeout kills; restart container if stuck.")
    if "gap" in joined or "lost scan" in joined:
        suggestions.append("5m jobs share one scheduler thread; if gaps persist, consider splitting scheduler into per-job processes or raising MAX_GAP threshold.")
    if "price feed" in joined:
        suggestions.append("Check exchange API keys / network inside container; `docker exec aitrader python -c 'from traders.common.exchange import ...'` to test feed.")
    # Only the actual section-3 high-reject warning implies prompt tuning —
    # a job name that merely contains "llm" (e.g. llm-review-report) must not trigger it.
    if "high reject ratio" in all_text:
        suggestions.append("Review `app/llm_prompts.py` DEFAULT_PROMPTS — a 90%+ REJECT ratio often means the prompt threshold is too strict; check `llm_review_log` reasons for patterns.")
    if "approvals but 0 buy" in joined:
        suggestions.append("Order placement may be failing silently — check trading script logs under `/state/logs/jobs/` and exchange error handling.")
    return suggestions


def main() -> None:
    now = _now()
    issues: list[str] = []
    warns: list[str] = []
    lines: list[str] = []

    lines.append(f"🩺 AITrader Health Check — {now.astimezone().strftime('%Y-%m-%d %H:%M %Z')}")
    lines.append("")

    with get_conn() as conn:
        cur = conn.cursor()

        # ── 1. Cron health ──────────────────────────────────────────────
        cur.execute(
            """SELECT job_name, count(*) AS runs,
                      count(*) FILTER (WHERE status = 'error') AS errs,
                      max(started_at) AS last_run
               FROM cron_runs
               WHERE started_at > %s
               GROUP BY job_name ORDER BY job_name""",
            (now - timedelta(hours=24),),
        )
        per_job = {r[0]: {"runs": r[1], "errs": r[2], "last": r[3]} for r in cur.fetchall()}

        # Job registry vs DB enabled/mode
        cur.execute("SELECT name, enabled, mode, next_run_at FROM cron_jobs ORDER BY name")
        db_jobs = {r[0]: {"enabled": r[1], "mode": r[2], "next": r[3]} for r in cur.fetchall()}

        lines.append("## 1. Cron health (24h)")
        for job, meta in sorted(per_job.items()):
            if job in DAILY_EXCLUDED:
                continue  # weekly job — not expected daily

            interval = EXPECTED_INTERVAL.get(job, 86400)
            expected = max(1, round(86400 / interval))
            runs = meta["runs"]
            errs = meta["errs"]
            last = meta["last"]

            if runs == 0:
                if job in MARKET_WINDOWED:
                    lines.append(f"⏸️ {job}: 0 runs in 24h (market-windowed — OK if US market was closed)")
                else:
                    issues.append(f"⚠️ {job}: 0 runs in 24h (expected ~{expected}) — job likely stuck/disabled")
                continue

            # Staleness: last run older than N intervals
            if last and (now - last) > timedelta(seconds=interval * STALE_FACTOR):
                if job not in MARKET_WINDOWED:
                    issues.append(f"⚠️ {job}: stale — last run {last.isoformat()} (> {interval*STALE_FACTOR}s ago)")
                else:
                    lines.append(f"⏸️ {job}: last run {last.isoformat()} — market-windowed, OK")

            if errs > 0:
                issues.append(f"❌ {job}: {errs} error(s) in 24h ({runs} runs)")
                # Show the most recent error summary
                cur.execute(
                    """SELECT substr(summary,1,200) FROM cron_runs
                       WHERE job_name=%s AND status='error' ORDER BY started_at DESC LIMIT 1""",
                    (job,),
                )
                row = cur.fetchone()
                if row and row[0]:
                    warns.append(f"   last error ({job}): {row[0]}")

            # Expected runs met?
            if runs < expected * 0.5 and job not in MARKET_WINDOWED:
                warns.append(f"ℹ️ {job}: {runs} runs in 24h (expected ~{expected}) — investigate if market was open")

        # Jobs enabled in DB but missing from cron_runs entirely (never ran?)
        for job, meta in sorted(db_jobs.items()):
            if job not in per_job and meta["enabled"] and job in MARKET_WINDOWED:
                lines.append(f"⏸️ {job}: enabled but 0 runs (market-windowed, OK)")
        issues.extend(missing_run_issues(db_jobs, per_job))

        # ── 2. Missed ticks (5m jobs) ──────────────────────────────────
        lines.append("")
        lines.append("## 2. Missed ticks (5m jobs, 24h)")
        for job in sorted(FIVE_MIN_JOBS):
            if job in MARKET_WINDOWED:
                continue  # alpaca defers when market closed — gaps are expected
            cur.execute(
                """SELECT started_at FROM cron_runs WHERE job_name=%s
                   AND started_at > %s ORDER BY started_at""",
                (job, now - timedelta(hours=24)),
            )
            times = [r[0] for r in cur.fetchall()]
            if len(times) < 2:
                continue
            deltas = [(times[i + 1] - times[i]).total_seconds() for i in range(len(times) - 1)]
            gaps = [d for d in deltas if d > MAX_GAP_SECONDS]
            if gaps:
                issues.append(f"⚠️ {job}: {len(gaps)} gap(s) > {MAX_GAP_SECONDS//60}min (max {max(gaps)/60:.1f}min) — lost scan windows")
                for g in sorted(gaps, reverse=True)[:3]:
                    warns.append(f"   gap: {g/60:.1f}min")
            else:
                lines.append(f"✅ {job}: {len(times)} runs, no gaps >{MAX_GAP_SECONDS//60}min (avg {sum(deltas)/len(deltas):.0f}s)")

        # ── 3. LLM gate ────────────────────────────────────────────────
        lines.append("")
        lines.append("## 3. LLM gate (24h)")
        cur.execute(
            """SELECT verdict, count(*) FROM llm_review_log
               WHERE created_at > %s GROUP BY verdict ORDER BY verdict""",
            (now - timedelta(hours=24),),
        )
        verdicts = dict(cur.fetchall())
        total_reviews = sum(verdicts.values())
        rejects = verdicts.get("REJECT", 0)
        approves = verdicts.get("APPROVE", 0)
        holds = verdicts.get("HOLD", 0)
        lines.append(f"Reviews: {total_reviews} | APPROVE: {approves} | REJECT: {rejects} | HOLD: {holds}")

        if total_reviews == 0:
            warns.append("ℹ️ 0 LLM reviews in 24h — no candidates seen (could be quiet market OR broken signal pipeline)")
        elif approves == 0 and rejects > 0 and rejects / total_reviews > 0.9:
            warns.append("⚠️ High REJECT ratio (>90%) with 0 approvals — possible over-strict prompt or market regime mismatch")
        elif total_reviews < 5:
            warns.append(f"ℹ️ Very few reviews ({total_reviews}) — candidate generation may be weak")

        # ── 4. Price feed freshness ────────────────────────────────────
        lines.append("")
        lines.append("## 4. Price feed (max age)")
        cur.execute(
            """SELECT exchange, max(timestamp) FROM asset_prices
               WHERE timestamp > %s GROUP BY exchange ORDER BY exchange""",
            (now - timedelta(days=2),),
        )
        feed = dict(cur.fetchall())
        if not feed:
            issues.append("❌ asset_prices empty in 48h — price feed completely down")
        for exch, ts in sorted(feed.items()):
            age = (now - ts).total_seconds() / 60
            if age > 10:
                issues.append(f"❌ {exch} price feed stale: {age:.0f}min old (threshold 10min)")
            else:
                lines.append(f"✅ {exch}: {age:.0f}min old")

        # ── 5. Trades vs approvals ─────────────────────────────────────
        lines.append("")
        lines.append("## 5. Trades (24h)")
        cur.execute(
            """SELECT action, count(*) FROM trade_log
               WHERE timestamp > %s GROUP BY action ORDER BY action""",
            (now - timedelta(hours=24),),
        )
        trades = dict(cur.fetchall())
        buys = trades.get("BUY", 0)
        sells = trades.get("SELL", 0)
        lines.append(f"BUY: {buys} | SELL: {sells}")

        if approves > 0 and buys == 0:
            warns.append(f"⚠️ {approves} LLM approvals but 0 BUY trades — approvals not materializing (order placement broken?)")

        # ── 6. Modes ───────────────────────────────────────────────────
        lines.append("")
        lines.append("## 6. Modes (DB is truth)")
        for job, meta in sorted(db_jobs.items()):
            flag = "🔴" if meta["mode"] == "paused" else ("🟡" if meta["mode"] == "paper" else "🟢")
            lines.append(f"{flag} {job}: {meta['mode']} {'✅' if meta['enabled'] else '⏸️ disabled'}")

    # ── Summary ────────────────────────────────────────────────────────
    lines.append("")
    severity = "OK"
    if issues:
        severity = "CRIT"
        lines.append(f"🚨 **{len(issues)} issue(s) found**")
        lines.append("")
        lines.extend(f"- {i}" for i in issues)
    elif warns:
        severity = "WARN"
        lines.append(f"⚠️ **{len(warns)} warning(s)**")
        lines.append("")
        lines.extend(f"- {w}" for w in warns)
    else:
        lines.append("✅ All systems nominal.")

    # ── Fix suggestions ────────────────────────────────────────────────
    if issues:
        lines.append("")
        lines.append("## 🔧 Suggested fixes")
        lines.extend(f"- {s}" for s in fix_suggestions(issues, warns))

    report = "\n".join(lines)
    print(report)


if __name__ == "__main__":
    main()
