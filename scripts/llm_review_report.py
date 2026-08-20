#!/usr/bin/env python3
"""Weekly LLM trade-review re-evaluation report.

Scores the LLM gate's performance using llm-backfill data (was_correct),
and recommends whether to stay in shadow mode or re-enable the gate.

Runs as cron job `llm-review-report` (weekly, Sunday 10:00 Athens).
Toggle context: app_settings.llm.review_mode (shadow|gate|off).

Recommendation logic (conservative — needs a real sample before trusting):
  - sample < 50 scored verdicts            -> KEEP SHADOW (not enough data)
  - overall accuracy >= 55%                -> CONSIDER 'gate' (LLM adds value)
  - overall accuracy < 55%                 -> KEEP SHADOW (worse than ~coin flip)
Never changes the setting itself — the human decides.
"""

from __future__ import annotations

import os
import sys
from datetime import timedelta

import psycopg2
from dotenv import load_dotenv

MIN_SAMPLE = 50
RE_ENABLE_THRESHOLD = 55.0  # % overall accuracy needed to consider gating again


def _connect():
    load_dotenv("/home/pank/projects/aitrader/.env", override=False)
    conn = psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", 5432)),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        connect_timeout=5,
    )
    conn.autocommit = True
    return conn


def main() -> int:
    try:
        conn = _connect()
    except Exception as e:
        print(f"llm-review-report: DB connection failed: {e}", file=sys.stderr)
        return 1

    cur = conn.cursor()

    # Current gating mode
    try:
        cur.execute("SELECT value FROM app_settings WHERE key = 'llm.review_mode'")
        row = cur.fetchone()
        mode = (row[0] if row else "shadow") or "shadow"
    except Exception:
        mode = "shadow"

    # Scored verdicts (was_correct set by llm-backfill), last 7 days + all-time
    try:
        cur.execute(
            """SELECT verdict,
                      COUNT(*) FILTER (WHERE was_correct)        AS correct,
                      COUNT(*) FILTER (WHERE NOT was_correct)    AS wrong,
                      COUNT(*)                                   AS scored
               FROM llm_review_log
               WHERE was_correct IS NOT NULL
                 AND created_at >= NOW() - INTERVAL '7 days'
               GROUP BY verdict ORDER BY verdict"""
        )
        week_rows = cur.fetchall()
        cur.execute(
            """SELECT verdict,
                      COUNT(*) FILTER (WHERE was_correct)        AS correct,
                      COUNT(*) FILTER (WHERE NOT was_correct)    AS wrong,
                      COUNT(*)                                   AS scored
               FROM llm_review_log
               WHERE was_correct IS NOT NULL
               GROUP BY verdict ORDER BY verdict"""
        )
        all_rows = cur.fetchall()
    except Exception as e:
        print(f"llm-review-report: query failed: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    def _block(title: str, rows: list) -> tuple[str, int, int]:
        lines = [f"{title}"]
        total_scored = total_correct = 0
        for verdict, correct, wrong, scored in rows:
            acc = (correct / scored * 100) if scored else 0.0
            lines.append(f"  {verdict}: {acc:.0f}% ({correct}/{scored})")
            total_scored += scored
            total_correct += correct
        if not rows:
            lines.append("  (no scored verdicts yet)")
        return "\n".join(lines), total_scored, total_correct

    week_txt, w_n, w_ok = _block("Last 7 days:", week_rows)
    all_txt, a_n, a_ok = _block("All-time:", all_rows)

    if a_n >= MIN_SAMPLE:
        overall_acc = a_ok / a_n * 100
        if overall_acc >= RE_ENABLE_THRESHOLD:
            rec = (f"🟢 overall {overall_acc:.0f}% — consider RE-ENABLING the gate "
                   f"(llm.review_mode='gate')")
        else:
            rec = (f"🔴 overall {overall_acc:.0f}% (<{RE_ENABLE_THRESHOLD:.0f}%) — "
                   f"KEEP SHADOW, LLM still worse than coin flip")
    else:
        rec = (f"⚪ only {a_n}/{MIN_SAMPLE} scored verdicts — KEEP SHADOW, "
               f"need more data before judging")

    print("📊 LLM trade-review re-evaluation (weekly)\n"
          f"Current mode: {mode}\n\n{week_txt}\n\n{all_txt}\n\nRecommendation: {rec}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
