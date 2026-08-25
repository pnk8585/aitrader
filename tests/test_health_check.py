"""Regression tests for health-check false positives (weekly jobs, LLM suggestion)."""

from scripts.health_check import DAILY_EXCLUDED, fix_suggestions, missing_run_issues


def _db_job(enabled=True):
    return {"enabled": enabled, "mode": "live", "next": None}


def test_weekly_llm_review_report_has_no_missing_daily_run_issue():
    db_jobs = {"llm-review-report": _db_job(), "weekly-rethink": _db_job()}
    assert missing_run_issues(db_jobs, {}) == []
    assert "llm-review-report" in DAILY_EXCLUDED


def test_daily_job_without_runs_still_flagged():
    issues = missing_run_issues({"db-cleanup": _db_job()}, {})
    assert issues == ["⚠️ db-cleanup: enabled in DB but 0 cron_runs in 24h"]


def test_disabled_job_without_runs_not_flagged():
    assert missing_run_issues({"db-cleanup": _db_job(enabled=False)}, {}) == []


def test_generic_llm_job_issue_does_not_suggest_prompt_tuning():
    issues = ["⚠️ llm-review-report: enabled in DB but 0 cron_runs in 24h"]
    suggestions = fix_suggestions(issues, warns=[])
    assert any("docker logs aitrader" in s for s in suggestions)
    assert not any("DEFAULT_PROMPTS" in s for s in suggestions)


def test_high_reject_ratio_still_suggests_prompt_tuning():
    issues = ["❌ kraken-grid: 3 error(s) in 24h (288 runs)"]
    warns = ["⚠️ High REJECT ratio (>90%) with 0 approvals — possible over-strict prompt or market regime mismatch"]
    suggestions = fix_suggestions(issues, warns)
    assert any("DEFAULT_PROMPTS" in s for s in suggestions)
