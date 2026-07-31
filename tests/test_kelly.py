"""Kelly sizing must fall back to fixed fraction on small samples."""
from traders.common.kelly import kelly_fraction, kelly_position_size


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params):
        self.sql = sql

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return FakeCursor(self._rows)


def test_falls_back_below_100_trades():
    rows = [("SELL", 2.0)] * 60 + [("SELL", -1.0)] * 39  # 99 trades, strong edge
    size = kelly_position_size(FakeConn(rows), "x", entry=100.0, stop=98.0, balance=1000.0)
    assert size == 1000.0 * 0.25  # fixed-fraction fallback, not Kelly


def test_uses_kelly_at_100_trades():
    # Weak edge so Kelly lands BELOW the 0.25 cap — otherwise this test
    # couldn't tell the Kelly path from the fixed-fraction fallback.
    rows = [("SELL", 1.2)] * 52 + [("SELL", -1.0)] * 48  # 100 trades
    size = kelly_position_size(FakeConn(rows), "x", entry=100.0, stop=98.0, balance=1000.0)
    kf = kelly_fraction(0.52, 1.2, -1.0)         # = 0.12
    assert abs(size - 1000.0 * kf) < 1e-6
    assert size != 1000.0 * 0.25                 # provably not the fallback
