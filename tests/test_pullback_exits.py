"""Tests for pullback exit logic."""

from traders.strategies.pullback.exits import compute_effective_stop, should_exit_pullback


def test_hard_stop_triggers():
    sell, reason = should_exit_pullback(
        unrealized_plpc=-3.0,
        peak_plpc=1.0,
        age_hours=1.0,
        effective_stop=-2.0,
        trend_3h=1.0,
    )
    assert sell is True
    assert "Hard stop" in reason


def test_trailing_tp_triggers():
    sell, _ = should_exit_pullback(
        unrealized_plpc=2.0,
        peak_plpc=5.0,
        age_hours=2.0,
        effective_stop=-2.0,
        trend_3h=2.0,
    )
    assert sell is True


def test_hold_when_within_limits():
    sell, reason = should_exit_pullback(
        unrealized_plpc=1.0,
        peak_plpc=1.0,
        age_hours=1.0,
        effective_stop=-2.0,
        trend_3h=2.0,
    )
    assert sell is False
    assert reason == ""


def test_stop_tightens_on_bleeding_day():
    loose = compute_effective_stop(rng_6h=4.0, rpnl_today=0.0)
    tight = compute_effective_stop(rng_6h=4.0, rpnl_today=-3.0)
    assert abs(tight) < abs(loose)