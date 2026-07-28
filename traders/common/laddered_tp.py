"""Laddered take-profit: scale out of positions incrementally."""

from dataclasses import dataclass

# (plpc_threshold, sell_fraction)
DEFAULT_LADDERS = [
    (5.0, 0.25),
    (10.0, 0.25),
    (15.0, 0.25),
    (25.0, 0.25),
]


@dataclass
class TpLevel:
    threshold_pct: float
    sell_fraction: float


def get_next_tp_level(tp_level, ladders=None):
    """Return the next TpLevel to act on, or None if all levels exhausted.

    tp_level: index into ladder list (0 = first level not yet hit).
    ladders: list of (threshold_pct, sell_fraction) tuples.
    """
    if ladders is None:
        ladders = DEFAULT_LADDERS
    if tp_level >= len(ladders):
        return None
    threshold, fraction = ladders[tp_level]
    return TpLevel(threshold_pct=threshold, sell_fraction=fraction)


def should_take_partial_profit(unrealized_plpc, tp_level, ladders=None):
    """Check if we should take partial profit at current P&L.

    Returns (should_sell, fraction, reason).
    """
    if ladders is None:
        ladders = DEFAULT_LADDERS
    nxt = get_next_tp_level(tp_level, ladders)
    if nxt is None:
        return False, 0.0, ""
    if unrealized_plpc >= nxt.threshold_pct:
        return (
            True,
            nxt.sell_fraction,
            f"Ladder TP +{nxt.threshold_pct}% (sell {nxt.sell_fraction * 100:.0f}%)",
        )
    return False, 0.0, ""
