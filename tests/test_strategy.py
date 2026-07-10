"""Tests for daily strategy loader."""

import json

from traders.common.strategy import load_daily_strategy


def test_stale_strategy_rejected(tmp_path, monkeypatch):
    path = tmp_path / "daily_strategy.json"
    path.write_text(json.dumps({"date": "2020-01-01", "focus_pairs": []}))
    monkeypatch.setattr("traders.common.strategy.DAILY_STRATEGY_PATH", str(path))
    assert load_daily_strategy(pool_bases={"BTC", "ETH"}) is None


def test_normalizes_crypto_pairs(tmp_path, monkeypatch):
    from datetime import datetime
    import pytz

    today = datetime.now(pytz.timezone("Europe/Athens")).strftime("%Y-%m-%d")
    path = tmp_path / "daily_strategy.json"
    path.write_text(json.dumps({
        "date": today,
        "focus_pairs": ["btc/eur", "INVALID"],
        "avoid_pairs": ["ETH/EUR"],
        "pullback_adjustment": "cautious",
    }))
    monkeypatch.setattr("traders.common.strategy.DAILY_STRATEGY_PATH", str(path))
    data = load_daily_strategy(
        pool_bases={"BTC", "ETH"},
        adjustment_key="pullback_adjustment",
        valid_adjustments=("normal", "cautious", "skip"),
        default_adjustment="normal",
    )
    assert data["focus_pairs"] == ["BTC"]
    assert data["avoid_pairs"] == ["ETH"]
    assert data["pullback_adjustment"] == "cautious"