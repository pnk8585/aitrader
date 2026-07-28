"""Dollar-Cost Averaging entry logic.

On first signal: deploy 50% of intended position.
If price drops 3% from signal: deploy another 25%.
If price drops 6% from signal: deploy remaining 25%.
"""

from __future__ import annotations

DCA_LEVELS = [
    {"drop_pct": 0.0, "deploy_pct": 0.50},  # 50% on signal
    {"drop_pct": 3.0, "deploy_pct": 0.25},  # 25% if drops 3%
    {"drop_pct": 6.0, "deploy_pct": 0.25},  # 25% if drops 6%
]

MAX_DCA_LEVEL = len(DCA_LEVELS) - 1  # 2


def dca_entry_decision(signal_price: float, current_price: float,
                       dca_level: int, levels: list[dict] | None = None) -> float:
    """Return deploy fraction (0-1) if DCA buy is warranted, else 0.

    Args:
        signal_price: Price at which the original signal fired.
        current_price: Current market price.
        dca_level: Current DCA level (0, 1, or 2). Incremented after each fill.
        levels: Override DCA level definitions (for testing).

    Returns:
        deploy_pct (0.0 to 1.0) if a buy should happen, 0.0 otherwise.
    """
    if levels is None:
        levels = DCA_LEVELS

    if dca_level >= len(levels):
        return 0.0  # all levels exhausted

    level = levels[dca_level]
    drop_pct = (signal_price - current_price) / signal_price * 100

    if drop_pct >= level["drop_pct"]:
        return level["deploy_pct"]
    return 0.0


def dca_buy_qty(total_position_eur: float, deploy_pct: float,
                current_price: float) -> float:
    """Compute the asset quantity to buy for this DCA level.

    Args:
        total_position_eur: Full intended position size in EUR.
        deploy_pct: Fraction to deploy this round (from dca_entry_decision).
        current_price: Current price per unit.

    Returns:
        Quantity to buy (in base asset units).
    """
    if current_price <= 0:
        return 0.0
    eur_to_deploy = total_position_eur * deploy_pct
    return eur_to_deploy / current_price
