"""Regime detection thresholds."""

ADX_TREND_THRESHOLD = 25
ADX_RANGE_THRESHOLD = 20
VOL_CRISIS_THRESHOLD = 30
VOL_RANGE_THRESHOLD = 15
RET_TREND_THRESHOLD = 5

ADX_PERIOD = 14
VOL_WINDOW = 20
RET_WINDOW = 20

# Regime labels deliberately use daily closes.  ``asset_prices`` is commonly
# written every five minutes, so sample counts alone must never be described as
# days.
REGIME_PRICE_EXCHANGE = "kraken"
REGIME_DAYS = max(ADX_PERIOD + 1, VOL_WINDOW + 1, RET_WINDOW + 1)
