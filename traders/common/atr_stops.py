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


def compute_atr_from_prices(prices, period=14):
    """Volatility proxy from close prices only (no OHLC).

    Uses std of returns * price as a stand-in for ATR.
    """
    if not prices or len(prices) < period + 1:
        return None
    rets = [(prices[i] - prices[i - 1]) / prices[i - 1] for i in range(1, len(prices))]
    if not rets:
        return None
    mean_r = sum(rets) / len(rets)
    var = sum((r - mean_r) ** 2 for r in rets) / (len(rets) - 1)
    return (var ** 0.5) * prices[-1]


def compute_atr_stop(entry_price, atr, multiplier=2.0):
    """Stop-loss price based on ATR multiple below entry."""
    if atr <= 0:
        return entry_price
    return entry_price - atr * multiplier


def compute_atr_tp(entry_price, atr, multiplier=3.0):
    """Take-profit price based on ATR multiple above entry."""
    return entry_price + atr * multiplier


def should_move_to_breakeven(current_price, entry_price, atr, threshold_mult=2.0):
    """True when price has moved threshold_mult * ATR in profit — move stop to entry."""
    return (current_price - entry_price) >= threshold_mult * atr
