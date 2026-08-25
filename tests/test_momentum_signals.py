"""Parity contracts for shared momentum entry classification."""

import pytest

from scripts.backtest_strategy import check_momentum_entry
from traders.strategies.momentum.signals import momentum_signal


@pytest.mark.parametrize(
    ("daily", "hourly", "expected"),
    [
        (None, None, None),
        (2.99, 1.99, None),
        (3.0, 0.0, ("STRONG_MOMENTUM", 0.67, "daily")),
        (4.0, 0.0, ("STRONG_MOMENTUM", 0.67, "daily")),
        (5.0, 0.0, ("EXTREME_MOMENTUM", 1.0, "daily")),
        (0.0, 2.0, ("STRONG_MOMENTUM", 0.67, "hourly")),
        (0.0, 3.0, ("EXTREME_MOMENTUM", 1.0, "hourly")),
        (5.0, 3.0, ("EXTREME_MOMENTUM", 1.0, "daily")),
    ],
)
def test_shared_momentum_signal_tiers_and_tie_winner(daily, hourly, expected):
    from traders.strategies.momentum.signals import evaluate_momentum_signal

    result = evaluate_momentum_signal(daily, hourly)

    if expected is None:
        assert result is None
    else:
        assert (result.signal, result.multiplier, result.winner) == expected


def test_live_tuple_adapter_and_backtest_adapter_have_parity():
    class Cache:
        def get_momentum_over(self, *args, **kwargs):
            return 5.0

        def get_one_hour_momentum(self, *args, **kwargs):
            return 3.0

    backtest_signal = check_momentum_entry(Cache(), "BTC/EUR", object(), 100.0)

    assert momentum_signal(5.0, 3.0) == (backtest_signal["signal"], backtest_signal["mult"])
