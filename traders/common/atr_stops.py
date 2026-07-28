"""ATR-based stop-loss and take-profit calculations."""


def compute_atr(candles, period=14):
    """Compute ATR from a list of OHLCV candles.

    Each candle: [timestamp, open, high, low, close, volume].
    Returns float or None if not enough data.
    """
    if not candles or len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h = float(candles[i][2])
        lo = float(candles[i][3])
        pc = float(candles[i - 1][4])
        tr = max(h - lo, abs(h - pc), abs(lo - pc))
        trs.append(tr)
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def compute_atr_stop(entry_price, atr, multiplier=2.0):
    """Stop-loss price based on ATR multiple below entry."""
    return entry_price - atr * multiplier


def compute_atr_tp(entry_price, atr, multiplier=3.0):
    """Take-profit price based on ATR multiple above entry."""
    return entry_price + atr * multiplier


def should_move_to_breakeven(current_price, entry_price, atr, threshold_mult=2.0):
    """True when price has moved threshold_mult * ATR in profit — move stop to entry."""
    return (current_price - entry_price) >= threshold_mult * atr
