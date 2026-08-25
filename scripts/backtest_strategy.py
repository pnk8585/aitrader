#!/usr/bin/env python3
"""Replay historical asset_prices through strategy logic and report performance metrics."""

import argparse
import csv
import math
import os
import sys
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import psycopg2
from dotenv import load_dotenv

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
load_dotenv(os.path.join(ROOT, ".env"))

from traders.strategies.momentum.exits import should_exit_momentum
from traders.strategies.momentum.signals import evaluate_momentum_signal
from traders.strategies.momentum import config as MOMENTUM_CONFIG
from traders.strategies.pullback.exits import should_exit_pullback, compute_effective_stop
from traders.common.atr_stops import compute_atr_from_prices, compute_atr_stop, compute_atr_tp
from traders.common.kelly import kelly_fraction

# ── config mirrors ──────────────────────────────────────────────────────────
ROUND_TRIP_FEE_PCT = 0.52
DAILY_ENTRY_PCT = 3.0
HOURLY_ENTRY_PCT = 2.0
DAILY_WINDOW_MIN = 1440

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
MAX_ATR_PCT = 3.5

CRYPTO_PAIRS = [
    "BTC/EUR", "ETH/EUR", "SOL/EUR", "AVAX/EUR", "LINK/EUR",
    "XRP/EUR", "DOGE/EUR", "SUI/EUR", "NEAR/EUR", "RENDER/EUR",
    "ADA/EUR", "DOT/EUR",
]


def connect_db():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.getenv("DB_NAME", "aitrader"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD", ""),
        connect_timeout=10,
    )


# ── PriceCache ───────────────────────────────────────────────────────────────

class PriceCache:
    """In-memory price data indexed by symbol, sorted by timestamp."""

    def __init__(self, price_data):
        self._data = price_data
        self._times = {s: [t for t, _ in rows] for s, rows in price_data.items()}
        self._prices = {s: [p for _, p in rows] for s, rows in price_data.items()}

    def symbols(self):
        return list(self._data.keys())

    def _find_nearest(self, symbol, target_time):
        """Return the latest observation at or before target_time (never future data)."""
        times = self._times.get(symbol, [])
        if not times:
            return None
        idx = bisect_right(times, target_time) - 1
        if idx < 0:
            return None
        return (times[idx], self._prices[symbol][idx])

    def _prices_in_window(self, symbol, end_time, minutes):
        times = self._times.get(symbol, [])
        if not times:
            return []
        start = end_time - timedelta(minutes=minutes)
        li, ri = bisect_left(times, start), bisect_right(times, end_time)
        return [(times[i], self._prices[symbol][i]) for i in range(li, ri)]

    def price_at(self, symbol, timestamp):
        """Return the latest known price at timestamp, or None if none exists yet."""
        r = self._find_nearest(symbol, timestamp)
        return r[1] if r else None

    def get_one_hour_momentum(self, symbol, current_time, current_price):
        target = current_time - timedelta(minutes=60)
        r = self._find_nearest(symbol, target)
        if r is None or r[1] == 0:
            return None
        return (current_price - r[1]) / r[1] * 100

    def get_momentum_over(self, symbol, current_time, current_price, minutes=60):
        target = current_time - timedelta(minutes=minutes)
        r = self._find_nearest(symbol, target)
        if r is None or r[1] == 0:
            return None
        return (current_price - r[1]) / r[1] * 100

    def get_range_pct(self, symbol, current_time, minutes=360):
        window = self._prices_in_window(symbol, current_time, minutes)
        if len(window) < 6:
            return None
        prices = [p for _, p in window]
        lo, hi = min(prices), max(prices)
        return ((hi - lo) / lo * 100) if lo > 0 else None

    def get_recent_high(self, symbol, current_time, minutes=60):
        window = self._prices_in_window(symbol, current_time, minutes)
        return max((p for _, p in window), default=None)

    def get_recent_low(self, symbol, current_time, minutes=60):
        window = self._prices_in_window(symbol, current_time, minutes)
        return min((p for _, p in window), default=None)

    def detect_regime(self, symbol, current_time):
        """Simplified regime from 6h range and 3h trend."""
        rng = self.get_range_pct(symbol, current_time, 360)
        nearest = self._find_nearest(symbol, current_time)
        if nearest is None or rng is None:
            return "uncertain"
        cp = nearest[1]
        t3 = self.get_momentum_over(symbol, current_time, cp, 180)
        if rng > 10:
            return "crisis"
        if rng > 5:
            return "trending" if (t3 is not None and abs(t3) > 2) else "ranging"
        return "uncertain"


# ── entry signals ────────────────────────────────────────────────────────────
# Momentum tier classification is shared with live. Pullback and non-signal
# gates remain research simplifications; this replay does not import live loops.

def check_momentum_entry(cache, symbol, current_time, current_price, cfg=None):
    """daily_pct >= DAILY_ENTRY_PCT or hourly >= HOURLY_ENTRY_PCT, with tier."""
    cfg = cfg or {}
    daily_entry_pct = cfg.get("DAILY_ENTRY_PCT", DAILY_ENTRY_PCT)
    hourly_entry_pct = cfg.get("HOURLY_ENTRY_PCT", HOURLY_ENTRY_PCT)

    daily = cache.get_momentum_over(symbol, current_time, current_price, DAILY_WINDOW_MIN)
    hourly = cache.get_one_hour_momentum(symbol, current_time, current_price)

    classification = evaluate_momentum_signal(
        daily, hourly,
        daily_entry_pct=daily_entry_pct,
        hourly_entry_pct=hourly_entry_pct,
    )
    if classification is None:
        return None

    return {
        "strategy": "momentum",
        "symbol": symbol,
        "price": current_price,
        "signal": classification.signal,
        "mult": classification.multiplier,
        "daily": daily,
        "hourly": hourly,
    }


def check_pullback_entry(cache, symbol, current_time, current_price, cfg=None):
    """trend_3h >= TREND_3H_MIN_PCT, pullback >= PULLBACK_MIN_PCT, R:R check."""
    cfg = cfg or {}
    trend_3h_min_pct = cfg.get("TREND_3H_MIN_PCT", TREND_3H_MIN_PCT)
    pullback_min_pct = cfg.get("PULLBACK_MIN_PCT", PULLBACK_MIN_PCT)

    rng = cache.get_range_pct(symbol, current_time, VOL_WINDOW_MIN)
    if rng is None or rng < VOL_FLOOR_PCT:
        return None

    t3 = cache.get_momentum_over(symbol, current_time, current_price, TREND_3H_MIN)
    t6 = cache.get_momentum_over(symbol, current_time, current_price, TREND_6H_MIN)
    if t3 is None or t6 is None or t3 < trend_3h_min_pct or t6 <= 0:
        return None

    h1 = cache.get_one_hour_momentum(symbol, current_time, current_price)
    if h1 is not None and h1 > BLOWOFF_GUARD_1H_PCT:
        return None

    hi1h = cache.get_recent_high(symbol, current_time, 60)
    if hi1h is None or hi1h <= 0:
        return None
    pullback = (hi1h - current_price) / hi1h * 100.0
    if pullback < pullback_min_pct:
        return None

    hi6h = cache.get_recent_high(symbol, current_time, 360)
    if hi6h is None or hi6h <= 0:
        return None
    room_pct = (hi6h - current_price) / current_price * 100.0

    stop_dist = min(MAX_HARD_STOP_PCT, max(MIN_HARD_STOP_PCT, 0.5 * (rng or MIN_HARD_STOP_PCT * 2)))
    if room_pct < RR_MIN * stop_dist:
        return None

    score = t3 + 0.5 * rng + pullback - max(0.0, (h1 or 0.0) - BLOWOFF_GUARD_1H_PCT)
    return {
        "strategy": "pullback",
        "symbol": symbol,
        "price": current_price,
        "signal": "PULLBACK",
        "mult": 1.0,
        "t3": t3,
        "t6": t6,
        "rng": rng,
        "pullback": pullback,
        "score": score,
    }


# ── BacktestEngine ───────────────────────────────────────────────────────────

def load_prices(db_conn, exchange, start, end, symbols):
    """Load asset_prices into a dict of symbol -> [(timestamp, price), ...]."""
    cur = db_conn.cursor()

    # Convert symbols from "BTC/EUR" to "BTC" for DB lookup
    base_symbols = [s.split("/")[0] for s in symbols]
    placeholders = ",".join(["%s"] * len(base_symbols))

    query = (
        "SELECT symbol, timestamp, price FROM asset_prices "
        "WHERE exchange = %s AND symbol IN (" + placeholders + ")"
    )
    params = [exchange]
    params.extend(base_symbols)

    if start:
        query += " AND timestamp >= %s"
        params.append(start)
    if end:
        query += " AND timestamp <= %s"
        params.append(end)

    query += " ORDER BY symbol, timestamp ASC"

    cur.execute(query, params)
    rows = cur.fetchall()
    cur.close()

    # Map back from base symbol to pair symbol
    base_to_pair = {s.split("/")[0]: s for s in symbols}

    prices = defaultdict(list)
    for symbol_base, ts, price in rows:
        pair = base_to_pair.get(symbol_base)
        if pair:
            prices[pair].append((ts, float(price)))

    # Sort each list
    for s in prices:
        prices[s].sort(key=lambda x: x[0])

    return dict(prices)


def all_timestamps(price_data):
    """Sorted unique timestamps across all symbols."""
    ts_set = set()
    for rows in price_data.values():
        for t, _ in rows:
            ts_set.add(t)
    return sorted(ts_set)


def canonical_cycle_timestamps(price_data, cycle_minutes=5):
    """Return a shared UTC cycle grid spanning the observed source timestamps."""
    source_times = all_timestamps(price_data)
    if not source_times:
        return []
    first, last = source_times[0], source_times[-1]
    first = first.replace(second=0, microsecond=0)
    first -= timedelta(minutes=first.minute % cycle_minutes)
    last = last.replace(second=0, microsecond=0)
    last -= timedelta(minutes=last.minute % cycle_minutes)
    cycles = []
    current = first
    while current <= last:
        cycles.append(current)
        current += timedelta(minutes=cycle_minutes)
    return cycles


def compute_equity_returns(equity_curve):
    """Annualized Sharpe from 5-min interval returns. periods_per_year = 365 * 288."""
    if len(equity_curve) < 2:
        return 0.0
    rets = []
    for i in range(1, len(equity_curve)):
        if equity_curve[i - 1] > 0:
            rets.append((equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1])
    if not rets:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1) if len(rets) > 1 else 0.0
    std = var ** 0.5
    periods_per_year = 365 * 288
    annualized = mean * periods_per_year
    annualized_std = std * (periods_per_year ** 0.5)
    return annualized / annualized_std if annualized_std > 0 else 0.0


def max_drawdown(equity_curve):
    """Max drawdown as a fraction (0.15 = 15%)."""
    if not equity_curve:
        return 0.0
    peak, dd = equity_curve[0], 0.0
    for v in equity_curve[1:]:
        if v > peak:
            peak = v
        dd = max(dd, (peak - v) / peak)
    return dd


def backtest_engine(db_conn, exchange="kraken", start=None, end=None,
                    strategies=None, initial_balance=10000.0,
                    use_atr_stops=True, use_kelly_sizing=True,
                    use_laddered_tp=True, atr_multiplier=2.0, kelly_fraction=0.25,
                    entry_cfg=None, entry_fee_pct=0.26, exit_fee_pct=0.26,
                    slippage_pct=0.0, momentum_max_open=MOMENTUM_CONFIG.MAX_OPEN_MOMENTUM,
                    momentum_max_trades_per_day=MOMENTUM_CONFIG.MAX_TRADES_PER_DAY,
                    momentum_cooldown_min=MOMENTUM_CONFIG.COOLDOWN_MIN,
                    momentum_daily_loss_breaker_pct=MOMENTUM_CONFIG.DAILY_LOSS_BREAKER_PCT):
    """Run the backtest and return a metrics dict."""
    if min(entry_fee_pct, exit_fee_pct, slippage_pct) < 0:
        raise ValueError("entry fees, exit fees, and slippage must be non-negative")
    if strategies is None:
        strategies = ["momentum", "pullback"]

    symbols = CRYPTO_PAIRS
    print(f"Loading price data for {len(symbols)} symbols...")
    price_data = load_prices(db_conn, exchange, start, end, symbols)
    cache = PriceCache(price_data)
    source_timestamps = all_timestamps(price_data)
    timestamps = canonical_cycle_timestamps(price_data)

    print(f"Loaded {sum(len(v) for v in price_data.values())} rows, "
          f"{len(source_timestamps)} source timestamps / {len(timestamps)} canonical 5-minute cycles "
          f"across {len(price_data)} symbols.")

    # State
    positions = {}          # (symbol, strategy) -> position dict
    closed_trades = []      # list of closed trade summaries
    cash = initial_balance
    equity_curve = []       # equity at 5-min intervals
    last_equity_ts = None

    skipped_atr = 0
    skipped_vol = 0
    total_checks = 0
    momentum_entries = []
    momentum_entries_by_day = defaultdict(int)
    momentum_net_plpc_by_day = defaultdict(float)
    last_momentum_exit = {}

    entry_funcs = {
        "momentum": check_momentum_entry,
        "pullback": check_pullback_entry,
    }

    def portfolio_equity(mark_time):
        """Cash plus the current marked value of every still-open position."""
        return cash + sum(
            pos["total_qty"] * (cache.price_at(symbol, mark_time) or pos["cost_basis"])
            for (symbol, _), pos in positions.items()
        )

    def open_position(symbol, strat, ts, price, signal):
        """Apply the shared research sizing/ATR checks and open one position."""
        nonlocal cash, skipped_atr, skipped_vol
        window_prices = [p for _, p in cache._prices_in_window(symbol, ts, 14 * 5)]
        atr_at_entry = compute_atr_from_prices(window_prices, period=14)
        if atr_at_entry is None or atr_at_entry <= 0:
            skipped_atr += 1
            return False
        atr_pct = atr_at_entry / price * 100
        if atr_pct > MAX_ATR_PCT:
            skipped_vol += 1
            return False
        deploy = max(0.05, min(kelly_fraction, 0.25)) if use_kelly_sizing else 0.1
        size = min(cash / (1 + entry_fee_pct / 100), cash * deploy * signal["mult"])
        buy_price = price * (1 + slippage_pct / 100)
        entry_fee = size * entry_fee_pct / 100
        entry_cash_debit = size + entry_fee
        qty = size / buy_price if buy_price > 0 else 0.0
        if qty <= 0 or entry_cash_debit > cash:
            return False
        key = (symbol, strat)
        positions[key] = {
            "symbol": symbol, "strategy": strat,
            "entries": [{"price": buy_price, "qty": qty, "time": ts}],
            "cost_basis": buy_price, "total_qty": qty,
            "entry_notional": size, "entry_cash_debit": entry_cash_debit,
            "entry_fee": entry_fee, "peak_plpc": 0.0, "entry_time": ts,
            "atr_at_entry": atr_at_entry, "kelly_fraction_used": deploy,
            "tp_level": 0, "tp_sold_qty": 0.0, "dca_level": 0,
            "signal_price": buy_price, "regime_at_entry": cache.detect_regime(symbol, ts),
            "signal": signal["signal"],
        }
        cash -= entry_cash_debit
        return True

    n = len(timestamps)
    report_every = max(1, n // 20)
    for ti, ts in enumerate(timestamps):
        if ti % report_every == 0:
                print(f"  {ti}/{n} timestamps ({ti*100//n}%) — "
                  f"{len(positions)} open, {len(closed_trades)} closed, equity {portfolio_equity(ts):.2f}")

        for symbol in sorted(price_data.keys()):
            price = cache.price_at(symbol, ts)
            if price is None:
                continue

            # Check existing positions (exit checks)
            closed_keys = []
            for (psym, pstrat), pos in list(positions.items()):
                if psym != symbol:
                    continue

                # Update peak
                plpc = (price - pos["cost_basis"]) / pos["cost_basis"] * 100
                if plpc > pos["peak_plpc"]:
                    pos["peak_plpc"] = plpc

                # ATR stop
                atr_stop_pct = None
                atr = pos.get("atr_at_entry")
                if atr and atr > 0:
                    stop_price = compute_atr_stop(pos["cost_basis"], atr, atr_multiplier)
                    atr_stop_pct = (stop_price - pos["cost_basis"]) / pos["cost_basis"] * 100

                age_hours = (ts - pos["entry_time"]).total_seconds() / 3600

                # Exit check
                if pstrat == "momentum":
                    sell, reason = should_exit_momentum(
                        unrealized_plpc=plpc,
                        peak_plpc=pos["peak_plpc"],
                        age_hours=age_hours,
                        atr_stop_pct=atr_stop_pct if use_atr_stops else None,
                    )
                else:  # pullback
                    # compute effective_stop
                    rng_6h = cache.get_range_pct(symbol, ts, 360)
                    # rpnl_today: simplified — use 0 since we don't track daily PnL per-strategy
                    eff_stop = compute_effective_stop(rng_6h, 0)
                    trend_3h = cache.get_momentum_over(symbol, ts, price, 180)
                    sell, reason = should_exit_pullback(
                        unrealized_plpc=plpc,
                        peak_plpc=pos["peak_plpc"],
                        age_hours=age_hours,
                        effective_stop=eff_stop,
                        trend_3h=trend_3h,
                        atr_stop_pct=atr_stop_pct if use_atr_stops else None,
                    )

                if sell:
                    sell_price = price * (1 - slippage_pct / 100)
                    sale_proceeds = pos["total_qty"] * sell_price
                    exit_fee = sale_proceeds * exit_fee_pct / 100
                    net_proceeds = sale_proceeds - exit_fee
                    gross_pnl = sale_proceeds - pos["entry_notional"]
                    net_pnl = net_proceeds - pos["entry_cash_debit"]
                    cash += net_proceeds
                    closed_trades.append({
                        "symbol": psym,
                        "strategy": pstrat,
                        "entry_time": pos["entry_time"],
                        "exit_time": ts,
                        "entry_price": pos["cost_basis"],
                        "exit_price": sell_price,
                        "plpc": plpc,  # compatibility: raw price change before all costs
                        "gross_plpc": gross_pnl / pos["entry_notional"] * 100,
                        "net_plpc": net_pnl / pos["entry_cash_debit"] * 100,
                        "gross_pnl": gross_pnl,
                        "net_pnl": net_pnl,
                        "realized_pnl": net_pnl,
                        "entry_fee": pos["entry_fee"],
                        "exit_fee": exit_fee,
                        "holding_hours": age_hours,
                        "peak_plpc": pos["peak_plpc"],
                        "signal": pos.get("signal", ""),
                        "reason": reason,
                    })
                    if pstrat == "momentum":
                        last_momentum_exit[psym] = ts
                        momentum_net_plpc_by_day[ts.date()] += net_pnl / pos["entry_cash_debit"] * 100
                    closed_keys.append((psym, pstrat))

            for key in closed_keys:
                del positions[key]

            # Pullback remains a separate research strategy: it uses the shared
            # five-minute price grid but does not inherit momentum's live caps.
            total_checks += 1
            for strat in strategies:
                if strat != "pullback":
                    continue
                key = (symbol, strat)
                if key in positions:
                    continue  # already in a trade

                # ponytail: skip entry if already holding this symbol via other strategy
                if any(k[0] == symbol for k in positions):
                    continue

                entry_fn = entry_funcs.get(strat)
                if entry_fn is None:
                    continue
                signal = entry_fn(cache, symbol, ts, price, cfg=entry_cfg)
                if signal is None:
                    continue

                open_position(symbol, strat, ts, price, signal)

        # Momentum mirrors the live orchestration shape: scan all symbols in
        # one canonical cycle, rank candidates, and open only the best one.
        if "momentum" in strategies:
            day = ts.date()
            open_momentum = sum(1 for _, strat in positions if strat == "momentum")
            daily_net_pct = momentum_net_plpc_by_day[day]
            can_enter_momentum = (
                open_momentum < momentum_max_open
                and momentum_entries_by_day[day] < momentum_max_trades_per_day
                and daily_net_pct > momentum_daily_loss_breaker_pct
            )
            if can_enter_momentum:
                candidates = []
                for symbol in sorted(price_data):
                    if any(held_symbol == symbol for held_symbol, _ in positions):
                        continue
                    price = cache.price_at(symbol, ts)
                    if price is None:
                        continue
                    exited = last_momentum_exit.get(symbol)
                    if exited is not None and ts - exited < timedelta(minutes=momentum_cooldown_min):
                        continue
                    total_checks += 1
                    signal = check_momentum_entry(cache, symbol, ts, price, cfg=entry_cfg)
                    if signal is None:
                        continue
                    score = max(signal.get("daily") if signal.get("daily") is not None else -math.inf,
                                signal.get("hourly") if signal.get("hourly") is not None else -math.inf)
                    candidates.append((score, symbol, price, signal))
                if candidates:
                    _, symbol, price, signal = min(candidates, key=lambda item: (-item[0], item[1]))
                    if open_position(symbol, "momentum", ts, price, signal):
                        momentum_entries.append({"time": ts, "symbol": symbol, "score": signal.get("daily") if signal.get("daily") is not None else signal.get("hourly")})
                        momentum_entries_by_day[day] += 1

        # Record the portfolio after all transactions at this timestamp.
        if last_equity_ts is None or (ts - last_equity_ts) >= timedelta(minutes=5):
            equity_curve.append(portfolio_equity(ts))
            last_equity_ts = ts

    # Force-close remaining positions at last available price
    for (psym, pstrat), pos in list(positions.items()):
        last_price_rows = price_data.get(psym, [])
        if last_price_rows:
            final_price = last_price_rows[-1][1]
            plpc = (final_price - pos["cost_basis"]) / pos["cost_basis"] * 100
            sell_price = final_price * (1 - slippage_pct / 100)
            sale_proceeds = pos["total_qty"] * sell_price
            exit_fee = sale_proceeds * exit_fee_pct / 100
            net_proceeds = sale_proceeds - exit_fee
            gross_pnl = sale_proceeds - pos["entry_notional"]
            net_pnl = net_proceeds - pos["entry_cash_debit"]
            cash += net_proceeds
            age_hours = (timestamps[-1] - pos["entry_time"]).total_seconds() / 3600
            closed_trades.append({
                "symbol": psym,
                "strategy": pstrat,
                "entry_time": pos["entry_time"],
                "exit_time": timestamps[-1],
                "entry_price": pos["cost_basis"],
                "exit_price": sell_price,
                "plpc": plpc,  # compatibility: raw price change before all costs
                "gross_plpc": gross_pnl / pos["entry_notional"] * 100,
                "net_plpc": net_pnl / pos["entry_cash_debit"] * 100,
                "gross_pnl": gross_pnl,
                "net_pnl": net_pnl,
                "realized_pnl": net_pnl,
                "entry_fee": pos["entry_fee"],
                "exit_fee": exit_fee,
                "holding_hours": age_hours,
                "peak_plpc": pos["peak_plpc"],
                "signal": pos.get("signal", ""),
                "reason": "force-close (end of data)",
            })
    positions.clear()

    # Append final equity point
    final_equity = cash
    if equity_curve:
        equity_curve.append(final_equity)

    # Metrics
    win_trades = [t for t in closed_trades if t["net_pnl"] > 0]
    loss_trades = [t for t in closed_trades if t["net_pnl"] <= 0]
    total_pnl = final_equity - initial_balance
    gross_total_pnl = sum(t["gross_pnl"] for t in closed_trades)
    sharpe = compute_equity_returns(equity_curve)
    dd = max_drawdown(equity_curve)

    metrics = {
        "initial_balance": initial_balance,
        "final_cash": round(cash, 2),
        "final_equity": round(final_equity, 2),
        "total_pnl": round(total_pnl, 2),
        "gross_total_pnl": round(gross_total_pnl, 2),
        "total_pnl_pct": round(total_pnl / initial_balance * 100, 2),
        "total_trades": len(closed_trades),
        "win_count": len(win_trades),
        "loss_count": len(loss_trades),
        "win_rate": round(len(win_trades) / len(closed_trades) * 100, 2) if closed_trades else 0,
        "avg_win_plpc": round(sum(t["net_plpc"] for t in win_trades) / len(win_trades), 2) if win_trades else 0,
        "avg_loss_plpc": round(sum(t["net_plpc"] for t in loss_trades) / len(loss_trades), 2) if loss_trades else 0,
        "avg_holding_hours": round(sum(t["holding_hours"] for t in closed_trades) / len(closed_trades), 1) if closed_trades else 0,
        "sharpe_ratio": round(sharpe, 3),
        "max_drawdown_pct": round(dd * 100, 2),
        "skipped_atr": skipped_atr,
        "skipped_vol": skipped_vol,
        "total_checks": total_checks,
        "strategies": strategies,
        "exchange": exchange,
        "start": str(start) if start else "first",
        "end": str(end) if end else "last",
        "closed_trades": closed_trades,
        "equity_curve": equity_curve,
        "entry_fee_pct": entry_fee_pct,
        "exit_fee_pct": exit_fee_pct,
        "slippage_pct": slippage_pct,
        "momentum_entries": momentum_entries,
        "momentum_orchestration": {
            "cycle_minutes": 5,
            "max_open": momentum_max_open,
            "max_trades_per_day": momentum_max_trades_per_day,
            "cooldown_min": momentum_cooldown_min,
            "daily_loss_breaker_pct": momentum_daily_loss_breaker_pct,
            "unmodelled_gates": ["LLM/AI consult", "regime gates", "rotation logic"],
        },
    }
    return metrics


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Backtest trading strategies on historical data")
    parser.add_argument("--exchange", default="kraken")
    parser.add_argument("--start", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", help="End date (YYYY-MM-DD)")
    parser.add_argument("--strategy", default="both", choices=["momentum", "pullback", "both"])
    parser.add_argument("--initial-balance", type=float, default=10000.0)
    parser.add_argument("--atr-stops", action="store_true", default=True)
    parser.add_argument("--no-atr-stops", dest="atr_stops", action="store_false")
    parser.add_argument("--kelly", dest="kelly_sizing", action="store_true", default=True)
    parser.add_argument("--no-kelly", dest="kelly_sizing", action="store_false")
    parser.add_argument("--atr-multiplier", type=float, default=2.0)
    parser.add_argument("--entry-fee-pct", type=float, default=0.26)
    parser.add_argument("--exit-fee-pct", type=float, default=0.26)
    parser.add_argument("--slippage-pct", type=float, default=0.0)
    parser.add_argument("--momentum-max-open", type=int, default=MOMENTUM_CONFIG.MAX_OPEN_MOMENTUM)
    parser.add_argument("--momentum-max-trades-per-day", type=int, default=MOMENTUM_CONFIG.MAX_TRADES_PER_DAY)
    parser.add_argument("--momentum-cooldown-min", type=int, default=MOMENTUM_CONFIG.COOLDOWN_MIN)
    parser.add_argument("--momentum-daily-loss-breaker-pct", type=float,
                        default=MOMENTUM_CONFIG.DAILY_LOSS_BREAKER_PCT)
    parser.add_argument("--csv", help="Export trade log to CSV file")
    args = parser.parse_args()

    strategies = {"momentum": ["momentum"], "pullback": ["pullback"], "both": ["momentum", "pullback"]}[args.strategy]

    print(f"Backtest: {args.exchange} | {args.strategy} | {args.start or 'all'} -> {args.end or 'now'}")
    print(f"  balance={args.initial_balance}  atr_stops={args.atr_stops}  kelly={args.kelly_sizing}  "
          f"atr_mult={args.atr_multiplier}")
    print(f"  entry_fee={args.entry_fee_pct}%  exit_fee={args.exit_fee_pct}%  slippage={args.slippage_pct}%")
    print("  momentum cycles=5m "
          f"max_open={args.momentum_max_open} daily_cap={args.momentum_max_trades_per_day} "
          f"cooldown={args.momentum_cooldown_min}m loss_breaker={args.momentum_daily_loss_breaker_pct}%")

    db_conn = connect_db()
    metrics = backtest_engine(
        db_conn,
        exchange=args.exchange,
        start=args.start,
        end=args.end,
        strategies=strategies,
        initial_balance=args.initial_balance,
        use_atr_stops=args.atr_stops,
        use_kelly_sizing=args.kelly_sizing,
        atr_multiplier=args.atr_multiplier,
        entry_fee_pct=args.entry_fee_pct,
        exit_fee_pct=args.exit_fee_pct,
        slippage_pct=args.slippage_pct,
        momentum_max_open=args.momentum_max_open,
        momentum_max_trades_per_day=args.momentum_max_trades_per_day,
        momentum_cooldown_min=args.momentum_cooldown_min,
        momentum_daily_loss_breaker_pct=args.momentum_daily_loss_breaker_pct,
    )
    db_conn.close()

    # Print results table
    print()
    print("=" * 64)
    print(f"{'BACKTEST RESULTS':^64}")
    print("=" * 64)
    print(f"  Exchange:        {metrics['exchange']}")
    print(f"  Strategies:      {', '.join(metrics['strategies'])}")
    print(f"  Period:          {metrics['start']} -> {metrics['end']}")
    print(f"  Initial balance: {metrics['initial_balance']:,.2f} EUR")
    print(f"  Final equity:    {metrics['final_equity']:,.2f} EUR")
    print(f"  Gross P&L:       {metrics['gross_total_pnl']:+,.2f} EUR")
    print(f"  Net P&L:         {metrics['total_pnl']:+,.2f} EUR ({metrics['total_pnl_pct']:+.2f}%)")
    print("-" * 64)
    print(f"  Total trades:    {metrics['total_trades']}")
    print(f"  Wins:            {metrics['win_count']}  ({metrics['win_rate']:.1f}%)")
    print(f"  Losses:          {metrics['loss_count']}")
    print(f"  Avg net win P/L: {metrics['avg_win_plpc']:+.2f}%")
    print(f"  Avg net loss P/L:{metrics['avg_loss_plpc']:+.2f}%")
    print(f"  Avg hold:        {metrics['avg_holding_hours']:.1f}h")
    print("-" * 64)
    print(f"  Sharpe ratio:    {metrics['sharpe_ratio']:.3f}")
    print(f"  Max drawdown:    {metrics['max_drawdown_pct']:.2f}%")
    print("-" * 64)
    print(f"  Skipped (ATR):   {metrics['skipped_atr']}")
    print(f"  Skipped (vol):   {metrics['skipped_vol']}")
    print(f"  Entry checks:    {metrics['total_checks']}")
    print("  Replay gates:    LLM/AI consult, regime gates, and rotation logic are not modelled")
    print("=" * 64)

    if args.csv:
        trades = metrics["closed_trades"]
        with open(args.csv, "w", newline="") as f:
            if trades:
                writer = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
                writer.writeheader()
                writer.writerows(trades)
            else:
                f.write("# No trades\n")
        print(f"\nTrades exported to {args.csv}")


if __name__ == "__main__":
    main()
