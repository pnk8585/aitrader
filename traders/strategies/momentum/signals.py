"""Pure, shared momentum entry classification."""

from dataclasses import dataclass


@dataclass(frozen=True)
class MomentumSignal:
    """The selected momentum tier and the measurement that won the selection."""

    signal: str
    multiplier: float
    winner: str
    daily: float | None
    hourly: float | None


def evaluate_momentum_signal(
    daily: float | None,
    hourly: float | None,
    *,
    daily_entry_pct: float = 3.0,
    hourly_entry_pct: float = 2.0,
) -> MomentumSignal | None:
    """Classify already-computed momentum using the production tier semantics."""
    if not ((daily is not None and daily >= daily_entry_pct) or
            (hourly is not None and hourly >= hourly_entry_pct)):
        return None

    if daily is not None and daily >= 5.0:
        daily_signal, daily_multiplier = "EXTREME_MOMENTUM", 1.0
    elif daily is not None and daily >= 3.0:
        daily_signal, daily_multiplier = "STRONG_MOMENTUM", 0.67
    elif daily is not None and daily >= daily_entry_pct:
        daily_signal, daily_multiplier = "MODERATE_MOMENTUM", 0.33
    else:
        daily_signal, daily_multiplier = None, 0.0

    if hourly is not None and hourly >= 3.0:
        hourly_signal, hourly_multiplier = "EXTREME_MOMENTUM", 1.0
    elif hourly is not None and hourly >= 2.0:
        hourly_signal, hourly_multiplier = "STRONG_MOMENTUM", 0.67
    elif hourly is not None and hourly >= hourly_entry_pct:
        hourly_signal, hourly_multiplier = "MODERATE_MOMENTUM", 0.33
    else:
        hourly_signal, hourly_multiplier = None, 0.0

    if hourly_multiplier > daily_multiplier:
        return MomentumSignal(hourly_signal, hourly_multiplier, "hourly", daily, hourly)
    if daily_signal is not None:
        return MomentumSignal(daily_signal, daily_multiplier, "daily", daily, hourly)
    if hourly_signal is not None:
        return MomentumSignal(hourly_signal, hourly_multiplier, "hourly", daily, hourly)
    return None


def momentum_signal(
    daily: float | None,
    hourly: float | None,
    *,
    daily_entry_pct: float = 3.0,
    hourly_entry_pct: float = 2.0,
) -> tuple[str | None, float]:
    """Compatibility adapter for live callers that expect a tuple."""
    result = evaluate_momentum_signal(
        daily, hourly,
        daily_entry_pct=daily_entry_pct,
        hourly_entry_pct=hourly_entry_pct,
    )
    return (result.signal, result.multiplier) if result else (None, 0.0)
