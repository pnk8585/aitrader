"""Momentum exit decision logic."""

from traders.strategies.momentum import config as C


def should_exit_momentum(
    *,
    unrealized_plpc: float,
    peak_plpc: float,
    age_hours: float,
    atr_stop_pct: float | None = None,
    tp_level: int = 0,
    tp_sold_qty: float = 0.0,
) -> tuple[bool, str]:
    """Return (sell, reason) for a momentum position."""
    if C.USE_ATR_STOPS and atr_stop_pct is not None and unrealized_plpc <= atr_stop_pct:
        return True, f"ATR stop ({round(unrealized_plpc, 2)}% <= {round(atr_stop_pct, 2)}%)"
    if C.USE_LADDERED_TP:
        from traders.common.laddered_tp import should_take_partial_profit
        take, fraction, reason = should_take_partial_profit(unrealized_plpc, tp_level)
        if take:
            return True, reason
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
    if age_hours >= C.MAX_HOLD_HOURS and unrealized_plpc <= C.ROUND_TRIP_FEE_PCT:
        return True, f"Max-hold time-stop ({round(age_hours, 1)}h)"
    return False, ""


def is_stale_rotation_candidate(unrealized_plpc: float, age_hours: float) -> bool:
    return (
        (age_hours >= C.STALE_FLAT_HOURS and unrealized_plpc < C.STALE_FLAT_PLPC)
        or age_hours >= C.STALE_MAX_HOURS
    )