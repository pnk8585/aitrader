"""Kelly sizing must fall back to fixed fraction on small samples."""
import pytest

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
        self.cursor_calls = 0

    def cursor(self):
        self.cursor_calls += 1
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


def test_measured_negative_edge_returns_zero_at_minimum_history():
    rows = [("SELL", 1.0)] * 40 + [("SELL", -1.0)] * 60
    assert kelly_position_size(FakeConn(rows), "x", 100.0, 98.0, 1000.0) == 0.0


def test_invalid_stop_returns_zero_after_history_exists():
    rows = [("SELL", 1.2)] * 52 + [("SELL", -1.0)] * 48
    assert kelly_position_size(FakeConn(rows), "x", 100.0, 100.0, 1000.0) == 0.0
    assert kelly_position_size(FakeConn(rows), "x", 100.0, 101.0, 1000.0) == 0.0


@pytest.mark.parametrize("rows", [[], [("SELL", 1.0)] * 99], ids=["zero_history", "99_sells"])
@pytest.mark.parametrize(
    "entry, stop, balance, fraction",
    [
        (100.0, 100.0, 1000.0, 0.25),  # stop == entry
        (100.0, 101.0, 1000.0, 0.25),  # stop > entry
        (0.0, -1.0, 1000.0, 0.25),     # entry <= 0
        (100.0, 98.0, 0.0, 0.25),      # balance <= 0
        (100.0, 98.0, 1000.0, 0.0),    # nonpositive configured fraction
    ],
)
def test_invalid_structural_inputs_return_zero_before_history_query(rows, entry, stop, balance, fraction):
    conn = FakeConn(rows)

    assert kelly_position_size(conn, "x", entry, stop, balance, fraction=fraction) == 0.0
    assert conn.cursor_calls == 0


def test_kelly_result_is_capped_by_configured_fraction():
    rows = [("SELL", 10.0)] * 90 + [("SELL", -1.0)] * 10
    assert kelly_position_size(FakeConn(rows), "x", 100.0, 98.0, 1000.0, fraction=0.10) == 100.0
