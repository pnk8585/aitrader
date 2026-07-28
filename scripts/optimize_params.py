#!/usr/bin/env python3
"""Grid-search parameter optimizer for the backtest engine.

Usage:
  python scripts/optimize_params.py --exchange kraken --start 2025-01-01 --end 2025-12-31
  python scripts/optimize_params.py --strategy momentum --csv optimization_results.csv
"""

import argparse
import csv
import itertools
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backtest_strategy import backtest_engine
from traders.extreme.db_prices import get_connection

import traders.strategies.pullback.config as PC
import traders.strategies.momentum.config as MC

PARAM_GRID = {
    "DAILY_ENTRY_PCT":   [2.0, 3.0, 4.0, 5.0],
    "HOURLY_ENTRY_PCT":  [1.0, 2.0, 3.0, 4.0],
    "TREND_3H_MIN_PCT":  [1.0, 2.0, 3.0, 4.0, 5.0],
    "PULLBACK_MIN_PCT":  [1.0, 2.0, 3.0, 4.0, 5.0],
    "ATR_STOP_MULTIPLIER": [1.5, 2.0, 2.5, 3.0],
    "KELLY_FRACTION":    [0.10, 0.15, 0.20, 0.25],
}

FEATURE_FLAGS = {
    "USE_ATR_STOPS": True,
    "USE_KELLY_SIZING": True,
    "USE_LADDERED_TP": True,
    "USE_REGIME_ROUTING": False,
    "USE_DCA_ENTRY": False,
}


@dataclass
class RunResult:
    params: dict
    trades: int
    win_rate: float
    avg_pnl: float
    max_dd: float
    sharpe: float
    strategy: str


def apply_params(params: dict) -> dict:
    """Override config module constants. Returns original values for restore."""
    original = {}
    for key, value in params.items():
        if hasattr(PC, key):
            original[("PC", key)] = getattr(PC, key)
            setattr(PC, key, value)
        if hasattr(MC, key):
            original[("MC", key)] = getattr(MC, key)
            setattr(MC, key, value)
    return original


def restore_params(original: dict) -> None:
    for (module, key), value in original.items():
        if module == "PC":
            setattr(PC, key, value)
        elif module == "MC":
            setattr(MC, key, value)


def apply_feature_flags() -> dict:
    original = {}
    for key, value in FEATURE_FLAGS.items():
        if hasattr(PC, key):
            original[("PC", key)] = getattr(PC, key)
            setattr(PC, key, value)
        if hasattr(MC, key):
            original[("MC", key)] = getattr(MC, key)
            setattr(MC, key, value)
    return original


def run_one(params: dict, db_conn, exchange: str, start: datetime | None,
            end: datetime | None, strategies: list[str], initial_balance: float) -> dict:
    # Note: KELLY_FRACTION is not in config modules — it's passed as a kwarg to backtest_engine
    original = apply_params(params)
    flag_original = apply_feature_flags()
    try:
        entry_cfg = {}
        for key in ("DAILY_ENTRY_PCT", "HOURLY_ENTRY_PCT", "TREND_3H_MIN_PCT", "PULLBACK_MIN_PCT"):
            if key in params:
                entry_cfg[key] = params[key]

        return backtest_engine(
            db_conn,
            exchange=exchange,
            start=start,
            end=end,
            strategies=strategies,
            initial_balance=initial_balance,
            use_atr_stops=FEATURE_FLAGS["USE_ATR_STOPS"],
            use_kelly_sizing=FEATURE_FLAGS["USE_KELLY_SIZING"],
            use_laddered_tp=FEATURE_FLAGS["USE_LADDERED_TP"],
            atr_multiplier=params.get("ATR_STOP_MULTIPLIER", 2.0),
            kelly_fraction=params.get("KELLY_FRACTION", 0.25),
            entry_cfg=entry_cfg,
        )
    finally:
        restore_params(original)
        restore_params(flag_original)


def build_combinations(strategy_filter: str) -> list[dict]:
    strategy_config = {
        "momentum": ["DAILY_ENTRY_PCT", "HOURLY_ENTRY_PCT", "ATR_STOP_MULTIPLIER", "KELLY_FRACTION"],
        "pullback": ["TREND_3H_MIN_PCT", "PULLBACK_MIN_PCT", "ATR_STOP_MULTIPLIER", "KELLY_FRACTION"],
    }

    if strategy_filter == "both":
        strategies = ["momentum", "pullback"]
    else:
        strategies = [strategy_filter]

    combos = []
    for st in strategies:
        keys = strategy_config[st]
        values = [PARAM_GRID[k] for k in keys]
        for combo in itertools.product(*values):
            params = dict(zip(keys, combo))
            params["_strategy"] = st
            combos.append(params)
    return combos


def print_best(results: list[RunResult]) -> None:
    if not results:
        print("No results.")
        return
    best = max(results, key=lambda r: r.sharpe)
    p = best.params
    print("\nBest Parameters (by Sharpe):")
    for k, v in p.items():
        if not k.startswith("_"):
            print(f"  {k}: {v}")
    print(f"\nPerformance:")
    print(f"  Strategy: {best.strategy}")
    print(f"  Trades: {best.trades}")
    print(f"  Win Rate: {best.win_rate:.1%}")
    print(f"  Avg P&L: {best.avg_pnl:+.2%}")
    print(f"  Max Drawdown: {best.max_dd:.2%}")
    print(f"  Sharpe Ratio: {best.sharpe:.2f}")


def print_comparison(results: list[RunResult]) -> None:
    atr_results = [r for r in results if r.params.get("ATR_STOP_MULTIPLIER") is not None]
    fixed_results = [r for r in results if r.params.get("ATR_STOP_MULTIPLIER") is None]
    kelly_results = [r for r in results if r.params.get("KELLY_FRACTION") is not None]
    fixed_size_results = [r for r in results if r.params.get("KELLY_FRACTION") is None]

    print("\n--- Comparison Tables ---")

    if atr_results:
        avg_atr_sharpe = sum(r.sharpe for r in atr_results) / len(atr_results)
        avg_atr_pnl = sum(r.avg_pnl for r in atr_results) / len(atr_results)
        print(f"\nAverage across all ATR-based runs:  Sharpe={avg_atr_sharpe:.2f}  Avg P&L={avg_atr_pnl:+.2%}")

    if kelly_results:
        avg_kelly_sharpe = sum(r.sharpe for r in kelly_results) / len(kelly_results)
        avg_kelly_pnl = sum(r.avg_pnl for r in kelly_results) / len(kelly_results)
        print(f"Average across all Kelly runs:      Sharpe={avg_kelly_sharpe:.2f}  Avg P&L={avg_kelly_pnl:+.2%}")

    # Fixed stop vs ATR comparison by ATR multiplier
    print("\nATR Multiplier breakdown:")
    for mult in sorted(set(r.params.get("ATR_STOP_MULTIPLIER") for r in results if r.params.get("ATR_STOP_MULTIPLIER"))):
        subset = [r for r in results if r.params.get("ATR_STOP_MULTIPLIER") == mult]
        if subset:
            avg = sum(r.sharpe for r in subset) / len(subset)
            avg_pnl = sum(r.avg_pnl for r in subset) / len(subset)
            print(f"  ATR {mult}x: Sharpe={avg:.2f}  Avg P&L={avg_pnl:+.2%}  (n={len(subset)})")

    print("\nKelly Fraction breakdown:")
    for frac in sorted(set(r.params.get("KELLY_FRACTION") for r in results if r.params.get("KELLY_FRACTION"))):
        subset = [r for r in results if r.params.get("KELLY_FRACTION") == frac]
        if subset:
            avg = sum(r.sharpe for r in subset) / len(subset)
            avg_pnl = sum(r.avg_pnl for r in subset) / len(subset)
            print(f"  Kelly {frac:.2f}: Sharpe={avg:.2f}  Avg P&L={avg_pnl:+.2%}  (n={len(subset)})")


def export_csv(results: list[RunResult], path: str) -> None:
    sorted_results = sorted(results, key=lambda r: r.sharpe, reverse=True)
    columns = ["rank", "strategy", "trend_3h", "pullback_pct", "atr_mult",
               "kelly_frac", "trades", "win_rate", "avg_pnl", "max_dd", "sharpe"]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(columns)
        for rank, r in enumerate(sorted_results, 1):
            w.writerow([
                rank,
                r.strategy,
                r.params.get("TREND_3H_MIN_PCT", ""),
                r.params.get("PULLBACK_MIN_PCT", ""),
                r.params.get("ATR_STOP_MULTIPLIER", ""),
                r.params.get("KELLY_FRACTION", ""),
                r.trades,
                round(r.win_rate, 4),
                round(r.avg_pnl, 4),
                round(r.max_dd, 4),
                round(r.sharpe, 4),
            ])
    print(f"\nResults written to {path} ({len(sorted_results)} rows)")


def main():
    p = argparse.ArgumentParser(description="Grid search parameter optimization")
    p.add_argument("--exchange", default="kraken")
    p.add_argument("--start", type=str, help="Start date (YYYY-MM-DD)")
    p.add_argument("--end", type=str, help="End date (YYYY-MM-DD)")
    p.add_argument("--strategy", choices=["momentum", "pullback", "both"], default="both")
    p.add_argument("--initial-balance", type=float, default=10000.0)
    p.add_argument("--csv", type=str, help="Export all results to CSV")
    args = p.parse_args()

    start_dt = datetime.strptime(args.start, "%Y-%m-%d") if args.start else None
    end_dt = datetime.strptime(args.end, "%Y-%m-%d") if args.end else None

    combos = build_combinations(args.strategy)
    total = len(combos)
    strategies_list = ["momentum", "pullback"] if args.strategy == "both" else [args.strategy]
    print(f"Running {total} parameter combinations for {args.strategy}...")

    conn = get_connection()
    results: list[RunResult] = []
    for i, combo in enumerate(combos):
        st = combo.pop("_strategy")
        print(f"  [{i+1}/{total}] {st}: {combo}", end=" ", flush=True)
        try:
            metrics = run_one(combo, conn, args.exchange, start_dt, end_dt,
                              [st], args.initial_balance)
            rr = RunResult(
                params=combo.copy(),
                trades=metrics.get("total_trades", 0),
                win_rate=metrics.get("win_rate", 0.0),
                avg_pnl=metrics.get("total_pnl_pct", 0.0),
                max_dd=metrics.get("max_drawdown_pct", 0.0),
                sharpe=metrics.get("sharpe_ratio", 0.0),
                strategy=st,
            )
            results.append(rr)
            print(f" -> Sharpe={rr.sharpe:.2f}")
        except Exception as exc:
            print(f" -> ERROR: {exc}")

    conn.close()

    print_best(results)
    print_comparison(results)

    if args.csv:
        export_csv(results, args.csv)


if __name__ == "__main__":
    main()
