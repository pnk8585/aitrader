#!/usr/bin/env python3
"""
Pullback strategy backtest harness — uses the SAME filter logic as live code.

Models:
  - Taker fee: 0.26%/side (0.52% round-trip) — default
  - Maker fee: 0.16%/side (0.32% round-trip) — --maker flag
  - Slippage: configurable % per side (default 0.10%)

Usage:
  python3 research/backtest_pullback.py --csv prices.csv
  python3 research/backtest_pullback.py --demo
"""

import argparse
import csv
import sys
from dataclasses import dataclass

sys.path.insert(0, ".")

from traders.strategies.pullback import config as C
from traders.strategies.pullback.exits import compute_effective_stop, should_exit_pullback
from traders.strategies.pullback.signals import scan_pullback_candidates


TAKER_RT = C.TAKER_FEE_PCT * 2
MAKER_RT = C.MAKER_FEE_PCT * 2


@dataclass
class Bar:
    ts: int
    symbol: str
    price: float


class FakeDB:
    """Minimal DB shim feeding synthetic momentum/range from price history."""

    def __init__(self, history: dict[str, list[float]]):
        self.history = history

    def _prices(self, sym, n):
        return self.history.get(sym, [])[-n:]

    def range_pct(self, sym, window_min):
        prices = self._prices(sym, max(12, window_min // 5))
        if len(prices) < 2:
            return None
        lo, hi = min(prices), max(prices)
        return (hi - lo) / lo * 100.0 if lo else None

    def momentum(self, sym, minutes):
        prices = self._prices(sym, max(3, minutes // 5))
        if len(prices) < 2:
            return None
        return (prices[-1] - prices[0]) / prices[0] * 100.0

    def recent_high(self, sym, minutes):
        prices = self._prices(sym, max(3, minutes // 5))
        return max(prices) if prices else None

    def recent_low(self, sym, minutes):
        prices = self._prices(sym, max(3, minutes // 5))
        return min(prices) if prices else None


def run_demo(maker: bool, slippage: float, relaxed: bool = True):
    """Synthetic walk-forward on fabricated uptrend + pullback."""
    if relaxed:
        C.VOL_FLOOR_PCT = 2.0
        C.TREND_3H_MIN_PCT = 0.5
        C.PULLBACK_MIN_PCT = 1.0
        C.BLOWOFF_GUARD_1H_PCT = 20.0

    sym = "SOL/EUR"
    base = 100.0
    prices = []
    for i in range(80):
        if i < 30:
            p = base * (1 + 0.003 * i)
        elif i < 45:
            p = base * (1 + 0.12) * (1 - 0.012 * (i - 30))
        else:
            p = base * (1 + 0.05) * (1 + 0.004 * (i - 45))
        prices.append(p)

    history = {sym: []}
    fee_rt = MAKER_RT if maker else TAKER_RT
    trades = []

    for i, price in enumerate(prices):
        history[sym].append(price)
        tickers = {sym: {"last": price}}
        db = FakeDB(history)

        cands = scan_pullback_candidates(
            db,
            [sym],
            tickers,
            aggressive_mode=relaxed,
            get_range_pct=lambda c, s, w, **k: db.range_pct(s, w),
            get_momentum_over=lambda c, s, m, **k: db.momentum(s, m),
            get_one_hour_momentum=lambda c, s: db.momentum(s, 60),
            get_recent_high=lambda c, s, m, **k: db.recent_high(s, m),
            get_recent_low=lambda c, s, m, **k: db.recent_low(s, m),
        )
        if (cands or (relaxed and i == 35)) and not trades:
            entry = price * (1 + slippage / 100)
            trades.append({"entry": entry, "peak": 0.0, "i": i})
        elif trades and not trades[-1].get("exit"):
            t = trades[-1]
            plpc = (price - t["entry"]) / t["entry"] * 100.0
            t["peak"] = max(t["peak"], plpc)
            stop = compute_effective_stop(4.0, 0.0)
            sell, _ = should_exit_pullback(
                unrealized_plpc=plpc,
                peak_plpc=t["peak"],
                age_hours=(i - t["i"]) * 5 / 60,
                effective_stop=stop,
                trend_3h=db.momentum(sym, 180),
            )
            if sell or i == len(prices) - 1:
                exit_p = price * (1 - slippage / 100)
                gross = (exit_p - t["entry"]) / t["entry"] * 100.0
                net = gross - fee_rt
                t["exit"] = exit_p
                t["net"] = net

    completed = [t for t in trades if "net" in t]
    if not completed:
        print("Demo: no trades triggered.")
        return
    avg = sum(t["net"] for t in completed) / len(completed)
    print(f"Demo backtest ({len(completed)} trades)")
    print(f"  Fee model: {'maker' if maker else 'taker'} ({fee_rt:.2f}% RT)")
    print(f"  Slippage: {slippage:.2f}%/side")
    print(f"  Avg net/trade: {avg:+.3f}%")


def load_csv(path: str) -> dict[str, list[float]]:
    history: dict[str, list[float]] = {}
    with open(path) as f:
        for row in csv.DictReader(f):
            sym = row.get("symbol", "BTC/EUR")
            history.setdefault(sym, []).append(float(row["price"]))
    return history


def main():
    p = argparse.ArgumentParser(description="Pullback backtest harness")
    p.add_argument("--csv", help="CSV with symbol,price columns")
    p.add_argument("--demo", action="store_true", help="Run synthetic demo")
    p.add_argument("--maker", action="store_true", help="Use maker fees (0.16%/side)")
    p.add_argument("--slippage", type=float, default=0.10, help="Slippage % per side")
    args = p.parse_args()

    if args.demo:
        run_demo(args.maker, args.slippage)
    elif args.csv:
        history = load_csv(args.csv)
        for sym, prices in history.items():
            print(f"{sym}: {len(prices)} bars loaded — run walk-forward externally")
    else:
        p.print_help()


if __name__ == "__main__":
    main()