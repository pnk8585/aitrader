"""Momentum exit decision logic."""

from traders.strategies.momentum import config as C


def should_exit_momentum(
    *,
    unrealized_plpc: float,
    peak_plpc: float,
    age_hours: float,
) -> tuple[bool, str]:
    """Return (sell, reason) for a momentum position."""
    if peak_plpc >= C.TTP_PEAK_PCT and unrealized_plpc <= (peak_plpc - C.TTP_GIVEBACK_PCT):
        return True, (
            f"Trailing TP (peak +{round(peak_plpc, 2)}% -> +{round(unrealized_plpc, 2)}%)"
        )
    if peak_plpc >= C.PLOCK_PEAK_PCT and unrealized_plpc < C.PLOCK_FLOOR_PCT:
        return True, (
            f"Profit lock (peak +{round(peak_plpc, 2)}% -> +{round(unrealized_plpc, 2)}%)"
        )
    if unrealized_plpc <= C.STOP_LOSS_PCT:
        return True, f"Stop-loss ({round(unrealized_plpc, 2)}% <= {C.STOP_LOSS_PCT}%)"
    if peak_plpc >= C.BREAKEVEN_PEAK_PCT and unrealized_plpc <= C.ROUND_TRIP_FEE_PCT:
        return True, (
            f"Breakeven protection (peak +{round(peak_plpc, 2)}% -> "
            f"+{round(unrealized_plpc, 2)}%, fee floor +{C.ROUND_TRIP_FEE_PCT}%)"
        )
    if age_hours >= C.MAX_HOLD_HOURS:
        return True, f"Max-hold time-stop ({round(age_hours, 1)}h)"
    return False, ""


def is_stale_rotation_candidate(unrealized_plpc: float, age_hours: float) -> bool:
    return (
        (age_hours >= C.STALE_FLAT_HOURS and unrealized_plpc < C.STALE_FLAT_PLPC)
        or age_hours >= C.STALE_MAX_HOURS
    )