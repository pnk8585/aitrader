"""Tests for exchange helpers."""

from traders.common.exchange import extract_fill, spread_ok


class FakeExchange:
    def __init__(self, book=None):
        self._book = book

    def fetch_order_book(self, symbol, limit=5):
        return self._book


def test_extract_fill_prefers_average():
    res = {"average": 100.5, "filled": 2.0, "cost": 201.0}
    price, qty = extract_fill(res, 99.0)
    assert price == 100.5
    assert qty == 2.0


def test_extract_fill_computes_from_cost():
    res = {"filled": 4.0, "cost": 400.0}
    price, qty = extract_fill(res, 50.0)
    assert price == 100.0
    assert qty == 4.0


def test_extract_fill_fallback():
    price, qty = extract_fill({}, 42.0)
    assert price == 42.0
    assert qty is None


def test_spread_ok_rejects_wide_spread():
    ex = FakeExchange({"bids": [[100.0, 1]], "asks": [[101.0, 1]]})
    ok, sp = spread_ok(ex, "BTC/EUR", max_spread_pct=0.5)
    assert sp is not None
    assert ok is False


def test_spread_ok_accepts_tight_spread():
    ex = FakeExchange({"bids": [[100.0, 1]], "asks": [[100.1, 1]]})
    ok, sp = spread_ok(ex, "BTC/EUR", max_spread_pct=0.5)
    assert ok is True