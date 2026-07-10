"""Tests for momentum exit logic."""

from traders.strategies.momentum.exits import is_stale_rotation_candidate, should_exit_momentum


def test_stop_loss():
    sell, reason = should_exit_momentum(unrealized_plpc=-3.0, peak_plpc=1.0, age_hours=1.0)
    assert sell is True
    assert "Stop-loss" in reason


def test_stale_rotation_flat():
    assert is_stale_rotation_candidate(unrealized_plpc=0.5, age_hours=1.0) is True


def test_not_stale_when_profitable():
    assert is_stale_rotation_candidate(unrealized_plpc=2.0, age_hours=1.0) is False