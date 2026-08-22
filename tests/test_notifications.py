"""Tests for AITrader Telegram notification policy and formatting."""

from app.cron_orchestrator import format_job_notification
from app.notify import format_telegram_text
from scripts.llm_backfill import format_summary
from traders.common import llm_review


def test_llm_backfill_summary_is_readable():
    summary = format_summary({
        "processed": 5,
        "backfilled": 0,
        "partial": 5,
        "no_data": 0,
        "waiting": 0,
        "skipped": 0,
    })

    assert "📦 Επεξεργάστηκαν: 5" in summary
    assert "⏳ Μερικά δεδομένα (1h/6h): 5" in summary
    assert "processed=" not in summary


def test_job_notification_uses_named_title_and_spacing():
    assert format_job_notification("llm-backfill", "summary") == "🧠 LLM Backfill\n\nsummary"


def test_telegram_format_converts_bold_and_escapes_html():
    formatted = format_telegram_text("🛒 **Αγοράστηκε BTC**: <unsafe> & ready")

    assert formatted == "🛒 <b>Αγοράστηκε BTC</b>: &lt;unsafe&gt; &amp; ready"


def test_llm_verdict_notification_is_silent(monkeypatch):
    sent = []
    monkeypatch.setattr(llm_review, "send_telegram", lambda message: sent.append(message), raising=False)

    llm_review._notify_verdict(
        {"verdict": "APPROVE", "confidence": 85},
        "BTC/EUR",
        "kraken-pullback",
        100.0,
    )

    assert sent == []
