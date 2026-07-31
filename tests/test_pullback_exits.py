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


def test_trailing_exit_config_is_net_positive_after_fees():
    """Trailing TP must never be able to trigger below the fee floor.

    Worst-case trailing exit for a given peak is peak - giveback; that must
    stay above the 0.52% round-trip fee for every reachable peak >= arm level.
    """
    from traders.strategies.pullback import config as C

    assert C.TRAIL_ARM_PCT >= C.ROUND_TRIP_FEE_PCT + C.TRAIL_GIVEBACK_MIN_PCT

    for peak in [C.TRAIL_ARM_PCT, 3.0, 5.0, 10.0, 25.0, 50.0]:
        giveback = max(C.TRAIL_GIVEBACK_MIN_PCT, peak * C.TRAIL_GIVEBACK_FRAC)
        worst_exit = peak - giveback
        assert worst_exit > C.ROUND_TRIP_FEE_PCT, (
            f"peak +{peak}% can trail-exit at {worst_exit}% — below fees"
        )


def test_min_trade_meets_kraken_minimums():
    from traders.strategies.pullback import config as C
    assert C.MIN_TRADE_EUR >= 5.0


def test_no_ladder_params_in_pullback_exit():
    """Pullback has a +5% hard TP cap; laddered TP was dead weight (fired a
    misleading 'sell 25%' reason but caused a full sell). It must be gone."""
    import inspect
    from traders.strategies.pullback.exits import should_exit_pullback

    params = inspect.signature(should_exit_pullback).parameters
    assert "tp_level" not in params
    assert "tp_sold_qty" not in params


def test_full_tp_at_cap_has_honest_reason():
    sell, reason = should_exit_pullback(
        unrealized_plpc=5.5,
        peak_plpc=5.5,
        age_hours=1.0,
        effective_stop=-2.0,
        trend_3h=2.0,
    )
    assert sell is True
    assert "Take-profit cap" in reason

