"""Exchange helpers: fill extraction, spread guard, dry-run / paper order wrappers."""

import os
import sys
import uuid

from traders.common.config import DRY_RUN

_PAPER_MODE = os.environ.get("AITRADER_MODE") == "paper"


def extract_fill(res, fallback_price):
    """Recover average fill price and qty from a CCXT order response."""
    price, qty = None, None
    if isinstance(res, dict):
        for k in ("average", "price"):
            v = res.get(k)
            if v:
                price = float(v)
                break
        filled = res.get("filled")
        cost = res.get("cost")
        if filled:
            qty = float(filled)
        if price is None and cost and filled:
            price = float(cost) / float(filled)
    return (price or fallback_price), qty


def spread_pct(exchange, symbol) -> float | None:
    """Bid-ask spread as % of mid price. None if book unavailable."""
    try:
        book = exchange.fetch_order_book(symbol, limit=5)
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            return None
        bid = float(bids[0][0])
        ask = float(asks[0][0])
        mid = (bid + ask) / 2.0
        if mid <= 0:
            return None
        return (ask - bid) / mid * 100.0
    except Exception as e:
        print(f"spread_pct failed for {symbol}: {e}", file=sys.stderr)
        return None


def spread_ok(exchange, symbol, max_spread_pct: float) -> tuple[bool, float | None]:
    """Return (ok, spread_pct). Reject when spread eats expected edge."""
    sp = spread_pct(exchange, symbol)
    if sp is None:
        return True, None
    return sp <= max_spread_pct, sp


def _dry_order(side: str, symbol: str, qty: float, price_hint: float) -> dict:
    oid = f"dry-run-{uuid.uuid4().hex[:8]}"
    print(f"DRY_RUN: {side} {qty} {symbol} @ ~{price_hint}", file=sys.stderr)
    return {
        "id": oid,
        "average": price_hint,
        "price": price_hint,
        "filled": qty,
        "cost": qty * price_hint,
        "side": side,
        "symbol": symbol,
        "status": "closed",
    }


def _paper_order(side: str, symbol: str, qty: float, price_hint: float) -> dict:
    """Simulate an order for paper mode — no real money moves."""
    oid = f"paper-{uuid.uuid4().hex[:12]}"
    print(f"PAPER: {side} {qty} {symbol} @ ~{price_hint}", file=sys.stderr)
    return {
        "id": oid,
        "clientOrderId": oid,
        "average": price_hint,
        "price": price_hint,
        "filled": qty,
        "remaining": 0.0,
        "cost": qty * price_hint,
        "side": side,
        "symbol": symbol,
        "status": "closed",
        "info": {"paper": True},
    }


def market_buy(exchange, symbol: str, qty: float, price_hint: float):
    if DRY_RUN:
        return _dry_order("buy", symbol, qty, price_hint)
    if _PAPER_MODE:
        return _paper_order("buy", symbol, qty, price_hint)
    return exchange.create_market_buy_order(symbol, qty)


def market_sell(exchange, symbol: str, qty: float, price_hint: float):
    if DRY_RUN:
        return _dry_order("sell", symbol, qty, price_hint)
    if _PAPER_MODE:
        return _paper_order("sell", symbol, qty, price_hint)
    return exchange.create_market_sell_order(symbol, qty)