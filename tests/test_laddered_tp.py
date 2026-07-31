"""Ladder sequencing: levels advance, quantities clamp, exits stay full-exit-only."""
import inspect

from traders.common.laddered_tp import should_take_partial_profit
from traders.strategies.momentum.exits import should_exit_momentum


def test_ladder_advances_through_levels():
    total, sold, level = 1.0, 0.0, 0

    # +6% crosses the first rung (+5%, sell 25%)
    take, qty, reason = should_take_partial_profit(6.0, level, total, sold)
    assert take and abs(qty - 0.25) < 1e-9
    sold += qty
    level += 1

    # Still +6%: next rung is +10%, nothing to do
    take, qty, _ = should_take_partial_profit(6.0, level, total, sold)
    assert not take

    # +11% crosses the second rung
    take, qty, _ = should_take_partial_profit(11.0, level, total, sold)
    assert take and abs(qty - 0.25) < 1e-9


def test_ladder_never_oversells():
    # Nearly everything already sold: rung wants 25% but only 10% remains
    take, qty, _ = should_take_partial_profit(30.0, 3, total_qty=1.0, already_sold_qty=0.9)
    assert take
    assert qty <= 0.1 + 1e-9


def test_momentum_exit_is_full_exit_only():
    params = inspect.signature(should_exit_momentum).parameters
    assert "tp_level" not in params
    assert "tp_sold_qty" not in params
