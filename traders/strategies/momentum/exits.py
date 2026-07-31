"""Momentum exit decision logic."""

from traders.strategies.momentum import config as C


def should_exit_momentum(
    *,
    unrealized_plpc: float,
    peak_plpc: float,
    age_hours: float,
    atr_stop_pct: float | None = None,
    cfg=None,
) -> tuple[bool, str]:
    """Return (sell, reason) for a momentum position. Full exits only.

    cfg: any object with the momentum exit constants; defaults to the
    crypto momentum config so existing callers are unchanged.
    """
    c = cfg or C
    if c.USE_ATR_STOPS and atr_stop_pct is not None and unrealized_plpc <= atr_stop_pct:
        return True, f"ATR stop ({round(unrealized_plpc, 2)}% <= {round(atr_stop_pct, 2)}%)"
    if peak_plpc >= c.TTP_PEAK_PCT and unrealized_plpc <= (peak_plpc - c.TTP_GIVEBACK_PCT):
        return True, (
            f"Trailing TP (peak +{round(peak_plpc, 2)}% -> +{round(unrealized_plpc, 2)}%)"
        )
    if peak_plpc >= c.PLOCK_PEAK_PCT and unrealized_plpc < c.PLOCK_FLOOR_PCT:
        return True, (
            f"Profit lock (peak +{round(peak_plpc, 2)}% -> +{round(unrealized_plpc, 2)}%)"
        )
    if unrealized_plpc <= c.STOP_LOSS_PCT:
        return True, f"Stop-loss ({round(unrealized_plpc, 2)}% <= {c.STOP_LOSS_PCT}%)"
    if peak_plpc >= c.BREAKEVEN_PEAK_PCT and unrealized_plpc <= c.ROUND_TRIP_FEE_PCT:
        return True, (
            f"Breakeven protection (peak +{round(peak_plpc, 2)}% -> "
            f"+{round(unrealized_plpc, 2)}%, fee floor +{c.ROUND_TRIP_FEE_PCT}%)"
        )
    if age_hours >= c.MAX_HOLD_HOURS and unrealized_plpc <= c.ROUND_TRIP_FEE_PCT:
        return True, f"Max-hold time-stop ({round(age_hours, 1)}h)"
    return False, ""


def is_stale_rotation_candidate(unrealized_plpc: float, age_hours: float) -> bool:
    return (
        (age_hours >= C.STALE_FLAT_HOURS and unrealized_plpc < C.STALE_FLAT_PLPC)
        or age_hours >= C.STALE_MAX_HOURS
    )
