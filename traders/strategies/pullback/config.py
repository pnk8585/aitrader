"""Pullback strategy constants."""

import os

from traders.common.config import LOG_DIR

# Defaults only — runtime list is traders.common.universe.get_crypto_pairs()
from traders.common.universe import DEFAULT_CRYPTO_PAIRS as CRYPTO_PAIRS  # noqa: E402

EXCHANGE_NAME = "kraken-pullback"
PRICE_EXCHANGE = "kraken"

LOCK_FILE = os.path.join(LOG_DIR, "kraken_pullback.lock")

ROUND_TRIP_FEE_PCT = 0.52
MAKER_FEE_PCT = 0.16
TAKER_FEE_PCT = 0.26
MAX_SPREAD_PCT = 0.35

VOL_FLOOR_PCT = 8.0
VOL_WINDOW_MIN = 360
TREND_3H_MIN_PCT = 3.0
TREND_3H_MIN = 180
TREND_6H_MIN = 360

PULLBACK_MIN_PCT = 3.0
BLOWOFF_GUARD_1H_PCT = 4.0
RR_MIN = 2.0

MIN_HARD_STOP_PCT = 1.5
MAX_HARD_STOP_PCT = 4.0
TRAIL_ARM_PCT = 2.0            # was 0.5 — must clear fees (0.52%) + min giveback (1.0%)
TRAIL_GIVEBACK_FRAC = 0.25
TRAIL_GIVEBACK_MIN_PCT = 1.0
HARD_TP_CAP_PCT = 5.0
MAX_HOLD_HOURS = 12.0
STALE_HOLD_HOURS = 18.0

DEPLOY_FRACTION = 0.1
RISK_PER_TRADE_PCT = 4.0
CONSULT_DEPLOY_FRACTION = 0.5
CONSULT_MIN_SCORE = 3.0
MIN_TRADE_EUR = 5.0            # was 0.45 — below Kraken's real per-pair order minimums
MAX_OPEN_SMALL = 1
MAX_OPEN_LARGE = 2
EQUITY_TWO_POS = 400.0

COOLDOWN_MIN = 90
MAX_TRADES_PER_DAY = 4
DAILY_LOSS_BREAKER_PCT = -4.0

# Phase 1 feature flags
USE_ATR_STOPS = True
USE_KELLY_SIZING = True
USE_DCA_ENTRY = False
ATR_PERIOD = 14
ATR_STOP_MULTIPLIER = 2.0
ATR_TP_MULTIPLIER = 3.0

