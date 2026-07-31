"""Tests for the exchange-backed ATR helper."""
from traders.common.atr_stops import fetch_atr_pct


class FakeExchange:
    def __init__(self, candles):
        self._candles = candles

    def fetch_ohlcv(self, symbol, timeframe, limit):
        return self._candles


def _candle(ts, o, h, l, c):
    return [ts, o, h, l, c, 0.0]


def test_fetch_atr_pct_constant_range():
    # 20 candles, every bar high-low = 2, close = 100 -> ATR = 2 -> 2% of close
    candles = [_candle(i, 100, 101, 99, 100) for i in range(20)]
    atr = fetch_atr_pct(FakeExchange(candles), "BTC/EUR", period=14)
    assert atr is not None
    assert abs(atr - 2.0) < 0.01


def test_fetch_atr_pct_short_data_returns_none():
    candles = [_candle(i, 100, 101, 99, 100) for i in range(5)]
    assert fetch_atr_pct(FakeExchange(candles), "BTC/EUR", period=14) is None


def test_fetch_atr_pct_exchange_error_returns_none():
    class Boom:
        def fetch_ohlcv(self, *a, **k):
            raise RuntimeError("api down")

    assert fetch_atr_pct(Boom(), "BTC/EUR") is None
