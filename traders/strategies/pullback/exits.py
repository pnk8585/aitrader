"""Pullback exit decision logic."""

from traders.strategies.pullback import config as C


def should_exit_pullback(
    *,
    unrealized_plpc: float,
    peak_plpc: float,
    age_hours: float,
    effective_stop: float,
    trend_3h: float | None,
    atr_stop_pct: float | None = None,
    tp_level: int = 0,
    tp_sold_qty: float = 0.0,
) -> tuple[bool, str]:
    """Return (sell, reason) for a pullback position."""
    if C.USE_ATR_STOPS and atr_stop_pct is not None and unrealized_plpc <= atr_stop_pct:
        return True, f"ATR stop ({round(unrealized_plpc, 2)}% <= {round(atr_stop_pct, 2)}%)"
    if C.USE_LADDERED_TP:
        from traders.common.laddered_tp import should_take_partial_profit
        take, fraction, reason = should_take_partial_profit(unrealized_plpc, tp_level)
        if take:
            return True, reason
    if unrealized_plpc <= effective_stop:
        return True, f"Hard stop ({round(unrealized_plpc, 2)}% <= {round(effective_stop, 2)}%)"
    if unrealized_plpc >= C.HARD_TP_CAP_PCT:
        return True, f"Take-profit cap (+{round(unrealized_plpc, 2)}% >= +{C.HARD_TP_CAP_PCT}%)"
    if peak_plpc >= C.TRAIL_ARM_PCT and unrealized_plpc <= (
        peak_plpc - max(C.TRAIL_GIVEBACK_MIN_PCT, peak_plpc * C.TRAIL_GIVEBACK_FRAC)
    ):
        giveback = max(C.TRAIL_GIVEBACK_MIN_PCT, peak_plpc * C.TRAIL_GIVEBACK_FRAC)
        return True, (
            f"Trailing TP (peak +{round(peak_plpc, 2)}% -> +{round(unrealized_plpc, 2)}%, "
            f"giveback {round(giveback, 2)}%, net +{round(unrealized_plpc - C.ROUND_TRIP_FEE_PCT, 2)}%)"
        )
    if age_hours >= C.MAX_HOLD_HOURS and unrealized_plpc < 0:
        if trend_3h is not None and trend_3h < 0:
            return True, (
                f"Max-hold dead-bag exit ({round(age_hours, 1)}h, "
                f"{round(unrealized_plpc, 2)}%, 3h trend {round(trend_3h, 2)}%)"
            )
    if (
        age_hours >= C.STALE_HOLD_HOURS
        and C.ROUND_TRIP_FEE_PCT < unrealized_plpc < C.TRAIL_ARM_PCT
    ):
        if trend_3h is not None and trend_3h < 0:
            return True, (
                f"Stale-winner exit ({round(age_hours, 1)}h, "
                f"net +{round(unrealized_plpc - C.ROUND_TRIP_FEE_PCT, 2)}%, "
                f"3h trend {round(trend_3h, 2)}%)"
            )
    return False, ""


def compute_effective_stop(rng_6h: float | None, rpnl_today: float) -> float:
    """Dynamic stop distance (negative %)."""
    stop_tighten = 0.6 if rpnl_today <= -2.0 else 1.0
    return -min(
        C.MAX_HARD_STOP_PCT,
        max(C.MIN_HARD_STOP_PCT, 0.5 * (rng_6h or C.MIN_HARD_STOP_PCT * 2)),
    ) * stop_tighten