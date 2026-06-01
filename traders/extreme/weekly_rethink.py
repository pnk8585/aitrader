#!/usr/bin/env python3
"""
weekly_rethink.py — AITrader v2 Weekly Strategy Rethink

PURPOSE
-------
Runs every Sunday (via cron) to mine the accumulated `asset_prices` and
`trade_log` history and answer one question: *given what actually happened,
which strategy parameters should change for the coming week?*

It is **read-only**. It NEVER places orders, never mutates trade state, and
never edits the live strategy. Its only writes are two report artifacts:
  - a machine-readable recommendations JSON (for a future adaptive cycle), and
  - a human-readable markdown report.

The live cycle (`execute_kraken_cycle_v2.py`) MAY later read the JSON to apply
adaptive thresholds — but that wiring is intentionally NOT built here. Today the
script only *suggests*.

WHAT IT COMPUTES
----------------
Per coin, per ISO week:
  - mean hi-lo range %, directional bias (close vs open %)
  - win/loss rate, avg hold time for winners vs losers
  - best/worst entry hour (UTC), corr(entry trend, forward net return)
Across the whole period:
  - volatility compression/expansion, trend persistence, coin rotation in/out
    of the vol-floor zone, time-of-day entry clusters, fee efficiency
Parameter what-ifs (counterfactual replay against the real price path):
  - hard stop -2.0 vs -2.5, vol floor 1.8 vs 2.0, trend 1.0 vs 1.5, ...
  - for each candidate threshold, how many trades flip / get filtered and the
    net-PnL delta it would have produced.

SAFETY / ROBUSTNESS
-------------------
  - Idempotent; safe to run manually any time.
  - Gracefully degrades on tiny datasets (currently ~9h of prices): emits
    "Δεν υπάρχουν αρκετά δεδομένα ακόμα" instead of inventing signal.
  - Every counterfactual that lacks price-path coverage is counted as
    *indeterminate* and never silently dropped.

USAGE
-----
  python3 weekly_rethink.py                 # Greek Telegram summary -> stdout
  python3 weekly_rethink.py --format markdown
  python3 weekly_rethink.py --format json
  python3 weekly_rethink.py --format all
  python3 weekly_rethink.py --weeks 4       # limit to the last N ISO weeks

Artifacts are always written regardless of --format:
  ~/.hermes/cron/output/strategy_rethink_recommendations.json
  ~/.hermes/cron/output/strategy_rethink_report.md
"""

import os
import sys
import json
import math
import argparse
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import datetime, timezone, timedelta

# Reuse the exact same DB plumbing the live cycle uses.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_prices import get_connection, close_connection, base_symbol  # noqa: E402

# ---------------------------------------------------------------------------
# Strategy parameters — MIRRORED from execute_kraken_cycle_v2.py.
# Keep these in sync with the live cycle. They are the *current* values we are
# evaluating; the candidate grids below are the alternatives we test against.
# (We deliberately do NOT import the v2 module: importing it constructs a CCXT
#  client and sys.exit()s when Kraken creds are absent — bad for a cron.)
# ---------------------------------------------------------------------------
CRYPTO_PAIRS = ["BTC/EUR", "ETH/EUR", "SOL/EUR", "AVAX/EUR", "LINK/EUR",
                "XRP/EUR", "DOGE/EUR", "SUI/EUR", "NEAR/EUR", "RENDER/EUR",
                "ADA/EUR", "DOT/EUR"]
COINS = [base_symbol(p) for p in CRYPTO_PAIRS]

ROUND_TRIP_FEE_PCT = 0.52   # Kraken taker ~0.26%/side

CURRENT = {
    "VOL_FLOOR_PCT": 1.8,
    "VOL_WINDOW_MIN": 360,
    "TREND_3H_MIN_PCT": 1.0,
    "TREND_3H_MIN": 180,
    "TREND_6H_MIN": 360,
    "PULLBACK_MIN_PCT": 0.5,
    "BLOWOFF_GUARD_1H_PCT": 4.0,
    "HARD_STOP_PCT": -2.0,
    "TRAIL_ARM_PCT": 1.5,
    "TRAIL_GIVEBACK_PCT": 0.8,
    "HARD_TP_CAP_PCT": 6.0,
    "MAX_HOLD_HOURS": 12.0,
    "COOLDOWN_MIN": 90,
}

# Candidate grids for the what-if sweep (the spec's headline questions first).
CANDIDATES = {
    "HARD_STOP_PCT": [-1.5, -2.0, -2.5, -3.0],
    "VOL_FLOOR_PCT": [1.5, 1.8, 2.0, 2.5],
    "TREND_3H_MIN_PCT": [0.5, 1.0, 1.2, 1.5, 2.0],
    "TRAIL_GIVEBACK_PCT": [0.5, 0.8, 1.0, 1.2],
}

# Minimum samples before we trust a statistic at all.
MIN_PRICE_SAMPLES = 12          # per coin per window for any range/trend read
MIN_TRADES_FOR_STATS = 4        # below this, a week is "not enough data"
MIN_TRADES_FOR_CORR = 5

OUTPUT_DIR = os.path.expanduser("~/.hermes/cron/output")
RECO_PATH = os.path.join(OUTPUT_DIR, "strategy_rethink_recommendations.json")
REPORT_PATH = os.path.join(OUTPUT_DIR, "strategy_rethink_report.md")


# ===========================================================================
# Data loading
# ===========================================================================
def load_prices(conn):
    """Return {coin: ([ts,...], [price,...])} sorted ascending by ts.

    Two parallel arrays per coin so we can bisect by timestamp cheaply when
    replaying price paths for counterfactuals.
    """
    out = defaultdict(lambda: ([], []))
    if conn is None:
        return out
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, timestamp, price FROM asset_prices "
            "WHERE exchange='kraken' ORDER BY symbol, timestamp ASC")
        for sym, ts, price in cur.fetchall():
            if price is None:
                continue
            ts_arr, p_arr = out[sym.upper()]
            ts_arr.append(ts)
            p_arr.append(float(price))
    return out


def load_trades(conn):
    """Return chronological list of BUY/SELL dicts from trade_log."""
    if conn is None:
        return []
    rows = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT timestamp, action, ticker, momentum_pct, entry_price, "
            "current_price, unrealized_plpc, estimated_value "
            "FROM trade_log WHERE exchange='kraken' AND action IN ('BUY','SELL') "
            "ORDER BY timestamp ASC")
        for ts, action, ticker, mom, entry, cur_p, plpc, est in cur.fetchall():
            rows.append({
                "ts": ts,
                "action": action,
                "ticker": ticker,
                "coin": base_symbol(ticker) if ticker else None,
                "momentum_pct": float(mom) if mom is not None else None,
                "entry_price": float(entry) if entry is not None else None,
                "current_price": float(cur_p) if cur_p is not None else None,
                # stored as a fraction; convert to % gross return for SELL rows
                "plpc_pct": float(plpc) * 100.0 if plpc is not None else None,
                "value_eur": float(est) if est is not None else None,
            })
    return rows


def reconstruct_round_trips(trades):
    """FIFO-match BUYs to SELLs per coin into completed round-trips.

    A round-trip = one BUY followed by the next SELL on the same coin. Returns
    list of dicts with entry/exit ts, gross %, net % (gross - fee), hold hours,
    entry hour (UTC) and the entry momentum the cycle recorded.
    """
    open_buys = defaultdict(list)
    trips = []
    for t in trades:
        coin = t["coin"]
        if coin is None:
            continue
        if t["action"] == "BUY":
            open_buys[coin].append(t)
        elif t["action"] == "SELL":
            if not open_buys[coin]:
                continue  # SELL with no matching BUY in window — skip
            buy = open_buys[coin].pop(0)
            gross = t["plpc_pct"]
            if gross is None and buy["entry_price"] and t["current_price"]:
                gross = (t["current_price"] - buy["entry_price"]) / buy["entry_price"] * 100.0
            if gross is None:
                continue
            net = gross - ROUND_TRIP_FEE_PCT
            hold_h = (t["ts"] - buy["ts"]).total_seconds() / 3600.0
            trips.append({
                "coin": coin,
                "entry_ts": buy["ts"],
                "exit_ts": t["ts"],
                "entry_price": buy["entry_price"],
                "exit_price": t["current_price"],
                "entry_momentum": buy["momentum_pct"],
                "gross_pct": gross,
                "net_pct": net,
                "hold_hours": hold_h,
                "entry_hour": buy["ts"].astimezone(timezone.utc).hour,
                "is_winner": net > 0,
            })
    return trips


# ===========================================================================
# Price-path helpers (for counterfactual replay)
# ===========================================================================
def prices_in(prices, coin, t0, t1):
    """Return [(ts, price), ...] for `coin` with t0 <= ts <= t1."""
    ts_arr, p_arr = prices.get(coin, ([], []))
    if not ts_arr:
        return []
    lo = bisect_left(ts_arr, t0)
    hi = bisect_right(ts_arr, t1)
    return list(zip(ts_arr[lo:hi], p_arr[lo:hi]))


def range_pct_before(prices, coin, t0, window_min):
    """Hi-lo range % over [t0-window, t0]. None if too few samples."""
    pts = prices_in(prices, coin, t0 - timedelta(minutes=window_min), t0)
    if len(pts) < MIN_PRICE_SAMPLES:
        return None
    vals = [p for _, p in pts]
    lo, hi = min(vals), max(vals)
    if lo <= 0:
        return None
    return (hi - lo) / lo * 100.0


def trend_pct_before(prices, coin, t0, window_min):
    """% change of price at t0 vs price ~window_min earlier. None if missing."""
    ts_arr, p_arr = prices.get(coin, ([], []))
    if len(ts_arr) < 2:
        return None
    i_now = bisect_right(ts_arr, t0) - 1
    if i_now < 0:
        return None
    now_price = p_arr[i_now]
    target = t0 - timedelta(minutes=window_min)
    # nearest sample within +-15% of the window age
    lo_t = t0 - timedelta(minutes=int(window_min * 1.15))
    hi_t = t0 - timedelta(minutes=int(window_min * 0.85))
    lo = bisect_left(ts_arr, lo_t)
    hi = bisect_right(ts_arr, hi_t)
    if lo >= hi:
        return None
    best_i, best_d = None, None
    for i in range(lo, hi):
        d = abs((ts_arr[i] - target).total_seconds())
        if best_d is None or d < best_d:
            best_i, best_d = i, d
    if best_i is None or p_arr[best_i] == 0:
        return None
    return (now_price - p_arr[best_i]) / p_arr[best_i] * 100.0


def replay_exit(prices, coin, entry_price, entry_ts, exit_ts, stop_pct,
                trail_arm, trail_give, tp_cap, max_hold_h):
    """Replay v2 exit rules over the real price path; return exit return % (net).

    Returns (net_pct, covered) where covered=False means we had no price path
    to simulate (so the result is the actual recorded one, not a real sim).
    """
    # extend the window a little past the real exit so a wider stop has room
    path = prices_in(prices, coin, entry_ts,
                     exit_ts + timedelta(hours=max_hold_h))
    if not entry_price or len(path) < 2:
        return None, False
    peak = 0.0
    for ts, price in path:
        plpc = (price - entry_price) / entry_price * 100.0
        peak = max(peak, plpc)
        age_h = (ts - entry_ts).total_seconds() / 3600.0
        if plpc <= stop_pct:
            return plpc - ROUND_TRIP_FEE_PCT, True
        if plpc >= tp_cap:
            return plpc - ROUND_TRIP_FEE_PCT, True
        if peak >= trail_arm and plpc <= (peak - trail_give):
            return plpc - ROUND_TRIP_FEE_PCT, True
        if age_h >= max_hold_h and plpc < 0:
            return plpc - ROUND_TRIP_FEE_PCT, True
    # never triggered within coverage — exit at last observed price
    last_plpc = (path[-1][1] - entry_price) / entry_price * 100.0
    return last_plpc - ROUND_TRIP_FEE_PCT, True


# ===========================================================================
# Statistics
# ===========================================================================
def pearson(xs, ys):
    """Pearson correlation; None if degenerate."""
    n = len(xs)
    if n < MIN_TRADES_FOR_CORR:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def iso_week_key(ts):
    y, w, _ = ts.astimezone(timezone.utc).isocalendar()
    return f"{y}-W{w:02d}"


def safe_mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


# ===========================================================================
# Per-coin weekly stats
# ===========================================================================
def weekly_coin_stats(prices, trips, weeks_limit):
    """Build {week: {coin: {...stats...}}} plus per-coin price aggregates.

    Price-derived stats (range, bias) come from asset_prices; trade-derived
    stats (win rate, hold time, entry hours) come from the round-trips.
    """
    # ---- group prices by (week, coin) ----
    price_weeks = defaultdict(lambda: defaultdict(list))  # week -> coin -> [(ts,p)]
    for coin in COINS:
        ts_arr, p_arr = prices.get(coin, ([], []))
        for ts, p in zip(ts_arr, p_arr):
            price_weeks[iso_week_key(ts)][coin].append((ts, p))

    # ---- group trips by (week, coin) ----
    trip_weeks = defaultdict(lambda: defaultdict(list))
    for t in trips:
        trip_weeks[iso_week_key(t["entry_ts"])][t["coin"]].append(t)

    all_weeks = sorted(set(price_weeks) | set(trip_weeks))
    if weeks_limit:
        all_weeks = all_weeks[-weeks_limit:]

    result = {}
    for wk in all_weeks:
        coins_out = {}
        for coin in COINS:
            pts = sorted(price_weeks[wk].get(coin, []))
            ctrips = trip_weeks[wk].get(coin, [])
            if not pts and not ctrips:
                continue

            # ---- price stats ----
            rng = bias = None
            if len(pts) >= MIN_PRICE_SAMPLES:
                vals = [p for _, p in pts]
                lo, hi = min(vals), max(vals)
                rng = (hi - lo) / lo * 100.0 if lo > 0 else None
                first, last = pts[0][1], pts[-1][1]
                bias = (last - first) / first * 100.0 if first > 0 else None

            # ---- trade stats ----
            wins = [t for t in ctrips if t["is_winner"]]
            losses = [t for t in ctrips if not t["is_winner"]]
            coins_out[coin] = {
                "price_samples": len(pts),
                "range_pct": round(rng, 3) if rng is not None else None,
                "bias_pct": round(bias, 3) if bias is not None else None,
                "trades": len(ctrips),
                "wins": len(wins),
                "losses": len(losses),
                "win_rate": round(len(wins) / len(ctrips), 3) if ctrips else None,
                "net_pct_sum": round(sum(t["net_pct"] for t in ctrips), 3) if ctrips else None,
                "avg_hold_win_h": round(safe_mean([t["hold_hours"] for t in wins]), 2) if wins else None,
                "avg_hold_loss_h": round(safe_mean([t["hold_hours"] for t in losses]), 2) if losses else None,
            }
        result[wk] = coins_out
    return result, all_weeks


# ===========================================================================
# Pattern detection
# ===========================================================================
def detect_patterns(prices, trips, weekly, weeks):
    """Surface the six pattern families the spec asks for."""
    p = {}

    # --- 1. Volatility compression / expansion (week-over-week mean range) ---
    week_mean_range = {}
    for wk in weeks:
        ranges = [c["range_pct"] for c in weekly[wk].values()
                  if c["range_pct"] is not None]
        week_mean_range[wk] = round(safe_mean(ranges), 3) if ranges else None
    p["volatility_by_week"] = week_mean_range
    vol_trend = "unknown"
    vals = [week_mean_range[w] for w in weeks if week_mean_range.get(w) is not None]
    if len(vals) >= 2:
        vol_trend = "expanding" if vals[-1] > vals[0] else "compressing"
    p["volatility_trend"] = vol_trend

    # --- 2. Trend persistence: does a coin's +bias week beget another? ---
    persist_hits = persist_total = 0
    for coin in COINS:
        seq = [weekly[w].get(coin, {}).get("bias_pct") for w in weeks]
        seq = [(i, b) for i, b in enumerate(seq) if b is not None]
        for j in range(len(seq) - 1):
            if seq[j][0] + 1 != seq[j + 1][0]:
                continue  # not consecutive weeks
            if seq[j][1] > 0:
                persist_total += 1
                if seq[j + 1][1] > 0:
                    persist_hits += 1
    p["trend_persistence"] = {
        "uptrend_followups": persist_total,
        "continued": persist_hits,
        "rate": round(persist_hits / persist_total, 3) if persist_total else None,
    }

    # --- 3. Coin rotation: who is in / leaving the vol-floor zone (latest wk) ---
    floor = CURRENT["VOL_FLOOR_PCT"]
    rotation = {"in_zone": [], "out_zone": [], "entering": [], "leaving": []}
    if weeks:
        last = weeks[-1]
        prev = weeks[-2] if len(weeks) >= 2 else None
        for coin in COINS:
            r_now = weekly[last].get(coin, {}).get("range_pct")
            if r_now is None:
                continue
            in_now = r_now >= floor
            (rotation["in_zone"] if in_now else rotation["out_zone"]).append(
                {"coin": coin, "range_pct": r_now})
            if prev:
                r_prev = weekly[prev].get(coin, {}).get("range_pct")
                if r_prev is not None:
                    in_prev = r_prev >= floor
                    if in_now and not in_prev:
                        rotation["entering"].append(coin)
                    elif in_prev and not in_now:
                        rotation["leaving"].append(coin)
    rotation["in_zone"].sort(key=lambda x: x["range_pct"], reverse=True)
    p["coin_rotation"] = rotation

    # --- 4. Time-of-day clusters: mean net return by entry UTC hour ---
    by_hour = defaultdict(list)
    for t in trips:
        by_hour[t["entry_hour"]].append(t["net_pct"])
    hour_stats = {h: {"n": len(v), "mean_net": round(safe_mean(v), 3)}
                  for h, v in sorted(by_hour.items())}
    p["entry_hour_stats"] = hour_stats
    winners = [t["entry_hour"] for t in trips if t["is_winner"]]
    losers = [t["entry_hour"] for t in trips if not t["is_winner"]]
    p["winner_entry_hours"] = _hour_window(winners)
    p["loser_entry_hours"] = _hour_window(losers)

    # --- 5. Fee efficiency: % of round-trips whose GROSS cleared the fee ---
    if trips:
        cleared = sum(1 for t in trips if t["gross_pct"] >= ROUND_TRIP_FEE_PCT)
        p["fee_efficiency"] = {
            "round_trips": len(trips),
            "cleared_fee": cleared,
            "cleared_pct": round(cleared / len(trips) * 100.0, 1),
            "lost_to_fee_pct": round((len(trips) - cleared) / len(trips) * 100.0, 1),
        }
    else:
        p["fee_efficiency"] = None

    # --- 6. corr(entry trend momentum, forward net return) ---
    xs = [t["entry_momentum"] for t in trips if t["entry_momentum"] is not None]
    ys = [t["net_pct"] for t in trips if t["entry_momentum"] is not None]
    p["entry_momentum_vs_return_corr"] = (
        round(pearson(xs, ys), 3) if len(xs) >= MIN_TRADES_FOR_CORR else None)

    return p


def _hour_window(hours):
    """Compact a list of hours into a 'lo-hi UTC' string + the modal hour."""
    if not hours:
        return None
    return {"min_h": min(hours), "max_h": max(hours),
            "modal_h": max(set(hours), key=hours.count), "n": len(hours)}


# ===========================================================================
# Parameter sensitivity / what-if
# ===========================================================================
def parameter_what_if(prices, trips):
    """Counterfactual sweep over the candidate grids. Path-replay where data
    exists; everything else counted as indeterminate (never silently dropped).
    """
    out = {}

    # ----- HARD_STOP_PCT (+ trailing giveback) via full path replay -----
    out["HARD_STOP_PCT"] = _sweep_exit_param(prices, trips, "HARD_STOP_PCT")
    out["TRAIL_GIVEBACK_PCT"] = _sweep_exit_param(prices, trips, "TRAIL_GIVEBACK_PCT")

    # ----- Entry filters: VOL_FLOOR_PCT and TREND_3H_MIN_PCT -----
    out["VOL_FLOOR_PCT"] = _sweep_entry_filter(
        prices, trips, "VOL_FLOOR_PCT", "range",
        CURRENT["VOL_WINDOW_MIN"])
    out["TREND_3H_MIN_PCT"] = _sweep_entry_filter(
        prices, trips, "TREND_3H_MIN_PCT", "trend",
        CURRENT["TREND_3H_MIN"])

    return out


def _sweep_exit_param(prices, trips, param):
    """Replay each trip's exit with each candidate value of an exit param."""
    base = dict(CURRENT)
    results = []
    for cand in CANDIDATES[param]:
        flips_to_win = flips_to_loss = covered = indeterminate = 0
        net_delta = 0.0
        for t in trips:
            kw = dict(stop_pct=base["HARD_STOP_PCT"],
                      trail_arm=base["TRAIL_ARM_PCT"],
                      trail_give=base["TRAIL_GIVEBACK_PCT"],
                      tp_cap=base["HARD_TP_CAP_PCT"],
                      max_hold_h=base["MAX_HOLD_HOURS"])
            if param == "HARD_STOP_PCT":
                kw["stop_pct"] = cand
            elif param == "TRAIL_GIVEBACK_PCT":
                kw["trail_give"] = cand
            sim_net, cov = replay_exit(prices, t["coin"], t["entry_price"],
                                       t["entry_ts"], t["exit_ts"], **kw)
            if not cov or sim_net is None:
                indeterminate += 1
                continue
            covered += 1
            net_delta += (sim_net - t["net_pct"])
            if t["net_pct"] <= 0 and sim_net > 0:
                flips_to_win += 1
            elif t["net_pct"] > 0 and sim_net <= 0:
                flips_to_loss += 1
        results.append({
            "value": cand,
            "is_current": cand == CURRENT[param],
            "covered_trips": covered,
            "indeterminate_trips": indeterminate,
            "losers_to_winners": flips_to_win,
            "winners_to_losers": flips_to_loss,
            "net_pct_delta_vs_current": round(net_delta, 3),
        })
    return results


def _sweep_entry_filter(prices, trips, param, kind, window_min):
    """For each candidate threshold, decide which trips it would have filtered
    (by recomputing the entry condition from asset_prices) and tally the PnL
    of filtered-out vs retained trips.
    """
    results = []
    for cand in CANDIDATES[param]:
        filtered_losers = filtered_winners = retained = indeterminate = 0
        retained_net = filtered_net = 0.0
        for t in trips:
            metric = (range_pct_before(prices, t["coin"], t["entry_ts"], window_min)
                      if kind == "range"
                      else trend_pct_before(prices, t["coin"], t["entry_ts"], window_min))
            if metric is None:
                indeterminate += 1
                continue
            passes = metric >= cand
            if passes:
                retained += 1
                retained_net += t["net_pct"]
            else:
                filtered_net += t["net_pct"]
                if t["net_pct"] > 0:
                    filtered_winners += 1
                else:
                    filtered_losers += 1
        results.append({
            "value": cand,
            "is_current": cand == CURRENT[param],
            "retained_trips": retained,
            "retained_net_pct": round(retained_net, 3),
            "filtered_losers": filtered_losers,
            "filtered_winners": filtered_winners,
            "filtered_net_pct": round(filtered_net, 3),
            "indeterminate_trips": indeterminate,
        })
    return results


# ===========================================================================
# Recommendations
# ===========================================================================
def build_recommendations(what_if, trips, patterns):
    """Turn the sweeps into concrete suggested-value changes with rationale.

    Conservative: only recommends a change when the counterfactual has real
    coverage AND the change improves net PnL or filters net-negative trades.
    Confidence scales with how many trips actually informed the decision.
    """
    recos = {}
    n_trips = len(trips)
    base_conf = "low" if n_trips < MIN_TRADES_FOR_STATS else (
        "medium" if n_trips < 12 else "high")

    # --- exit params: pick the candidate with best net_pct_delta (>0) ---
    for param in ("HARD_STOP_PCT", "TRAIL_GIVEBACK_PCT"):
        rows = what_if[param]
        covered = [r for r in rows if r["covered_trips"] > 0]
        cur_val = CURRENT[param]
        if not covered:
            recos[param] = _no_change(cur_val, "Δεν υπάρχει κάλυψη τιμών (price path) για replay.", "low")
            continue
        best = max(covered, key=lambda r: r["net_pct_delta_vs_current"])
        if best["value"] != cur_val and best["net_pct_delta_vs_current"] > 0.05:
            recos[param] = {
                "current": cur_val, "suggested": best["value"], "change": True,
                "confidence": base_conf,
                "rationale": (f"Replay σε {best['covered_trips']} trades: net "
                              f"{best['net_pct_delta_vs_current']:+.2f}% vs τώρα, "
                              f"{best['losers_to_winners']} χαμένα→κερδισμένα."),
                "evidence": best,
            }
        else:
            recos[param] = _no_change(cur_val, "Η τρέχουσα τιμή είναι ήδη βέλτιστη στο replay.", base_conf)

    # --- entry filters: raise threshold if it nets out negative trades ---
    for param in ("VOL_FLOOR_PCT", "TREND_3H_MIN_PCT"):
        rows = what_if[param]
        informed = [r for r in rows if (r["retained_trips"] + r["filtered_losers"]
                                        + r["filtered_winners"]) > 0]
        cur_val = CURRENT[param]
        if not informed:
            recos[param] = _no_change(cur_val, "Δεν υπάρχουν αρκετά δεδομένα τιμών στην είσοδο.", "low")
            continue
        # prefer the threshold maximizing retained_net while filtering net<0 trades
        best = max(informed, key=lambda r: r["retained_net_pct"])
        cur_row = next((r for r in rows if r["value"] == cur_val), None)
        improves = cur_row is None or best["retained_net_pct"] > cur_row["retained_net_pct"] + 0.05
        net_filtered_is_bad = best["filtered_net_pct"] < 0
        if best["value"] != cur_val and improves and net_filtered_is_bad:
            recos[param] = {
                "current": cur_val, "suggested": best["value"], "change": True,
                "confidence": base_conf,
                "rationale": (f"Θα έκοβε {best['filtered_losers']} χαμένα / "
                              f"{best['filtered_winners']} κερδισμένα "
                              f"(net κομμένων {best['filtered_net_pct']:+.2f}%), "
                              f"retained net {best['retained_net_pct']:+.2f}%."),
                "evidence": best,
            }
        else:
            recos[param] = _no_change(cur_val, "Η τρέχουσα τιμή κρατάει το καλύτερο retained net.", base_conf)

    # --- momentum signal sanity flag ---
    corr = patterns.get("entry_momentum_vs_return_corr")
    if corr is not None and abs(corr) < 0.1:
        recos["_signal_warning"] = (
            f"corr(entry momentum, return)={corr:+.2f} — σχεδόν θόρυβος, "
            "όπως στο v1 postmortem. Το trend filter ίσως χρειάζεται επανεξέταση.")

    return recos, base_conf


def _no_change(cur_val, why, conf):
    return {"current": cur_val, "suggested": cur_val, "change": False,
            "confidence": conf, "rationale": why}


# ===========================================================================
# Rendering
# ===========================================================================
def fmt_date(dt):
    return f"{dt.day}/{dt.month}/{dt.year}"


def render_telegram(report):
    """Concise Greek Telegram block (what the cron forwards to stdout)."""
    if report["status"] == "insufficient_data":
        return (f"📊 **Εβδομαδιαία Ανασκόπηση AITrader v2** ({report['date']})\n\n"
                f"⚠️ Δεν υπάρχουν αρκετά δεδομένα ακόμα.\n"
                f"  • Price samples: {report['totals']['price_samples']}\n"
                f"  • Round-trips: {report['totals']['round_trips']}\n"
                f"  • Χρειάζονται ≥{MIN_TRADES_FOR_STATS} ολοκληρωμένα trades & "
                f"≥{MIN_PRICE_SAMPLES} samples/coin.\n"
                f"Η ανάλυση θα γίνει αξιόπιστη καθώς μαζεύονται δεδομένα.")

    pat = report["patterns"]
    tot = report["totals"]
    L = [f"📊 **Εβδομαδιαία Ανασκόπηση AITrader v2** ({report['date']})", ""]

    # performance
    fee = pat.get("fee_efficiency") or {}
    L.append("📈 **Απόδοση:**")
    L.append(f"  • Trades: {tot['round_trips']} | Wins: {tot['wins']} "
             f"({tot['win_rate_pct']}%) | Net: {tot['net_pct']:+.2f}%")
    if fee:
        L.append(f"  • Χαμένα λόγω fees: {fee['lost_to_fee_pct']}% των trades")
    L.append("")

    # hot coins (latest week, top by range)
    hot = report["hot_coins"][:4]
    if hot:
        L.append("🔥 **Hot coins αυτή την εβδομάδα:**")
        for h in hot:
            arrow = "↑" if (h.get("bias_pct") or 0) > 0 else "↓"
            L.append(f"  • {h['coin']}: range {h['range_pct']:.2f}% ({arrow})")
        for c in pat["coin_rotation"]["entering"][:3]:
            L.append(f"  • {c}: μπήκε στο vol zone")
        L.append("")

    # recommended changes
    changes = [(k, v) for k, v in report["recommendations"].items()
               if isinstance(v, dict) and v.get("change")]
    L.append("⚙️ **Προτεινόμενες αλλαγές:**")
    if changes:
        for k, v in changes:
            L.append(f"  • {_gr_param(k)} → {v['suggested']} ({v['rationale']})")
    else:
        L.append("  • Καμία — οι τρέχουσες παράμετροι κρατάνε το καλύτερο fit.")
    L.append("")

    # patterns
    L.append("🔍 **Patterns:**")
    w = pat.get("winner_entry_hours")
    l = pat.get("loser_entry_hours")
    if w:
        L.append(f"  • Οι νικητές μπαίνουν {w['min_h']}-{w['max_h']} UTC")
    if l:
        L.append(f"  • Οι χαμένοι μπαίνουν {l['min_h']}-{l['max_h']} UTC")
    L.append(f"  • Volatility: {_gr_voltrend(pat['volatility_trend'])}")
    corr = pat.get("entry_momentum_vs_return_corr")
    if corr is not None:
        L.append(f"  • corr(momentum, return): {corr:+.2f}")
    return "\n".join(L)


def _gr_param(k):
    return {"HARD_STOP_PCT": "hard_stop", "VOL_FLOOR_PCT": "vol_floor",
            "TREND_3H_MIN_PCT": "trend_3h_min",
            "TRAIL_GIVEBACK_PCT": "trail_giveback"}.get(k, k)


def _gr_voltrend(t):
    return {"expanding": "διευρύνεται ↑", "compressing": "συμπιέζεται ↓"}.get(t, "—")


def render_markdown(report):
    """Full human-readable markdown report."""
    L = [f"# Εβδομαδιαία Ανασκόπηση AITrader v2 — {report['date']}", "",
         f"- Generated (UTC): `{report['generated_utc']}`",
         f"- Status: **{report['status']}**",
         f"- Weeks covered: {', '.join(report['weeks']) or '—'}", ""]

    tot = report["totals"]
    L += ["## Σύνοψη", "",
          f"- Round-trips: **{tot['round_trips']}** "
          f"(wins {tot['wins']}, losses {tot['losses']}, "
          f"win-rate {tot['win_rate_pct']}%)",
          f"- Net PnL (μετά fees): **{tot['net_pct']:+.2f}%**",
          f"- Price samples: {tot['price_samples']}", ""]

    if report["status"] == "insufficient_data":
        L += ["> ⚠️ Δεν υπάρχουν αρκετά δεδομένα ακόμα — οι παρακάτω αριθμοί είναι "
              "ενδεικτικοί και θα γίνουν αξιόπιστοι καθώς μαζεύεται ιστορικό.", ""]

    # per-week per-coin table
    L += ["## Ανά εβδομάδα / coin", ""]
    for wk in report["weeks"]:
        coins = report["weekly"][wk]
        if not coins:
            continue
        L.append(f"### {wk}")
        L.append("| Coin | Range % | Bias % | Trades | Win% | Net % | Hold W/L (h) |")
        L.append("|------|--------:|-------:|-------:|-----:|------:|:------------:|")
        for coin, c in sorted(coins.items()):
            L.append("| {coin} | {r} | {b} | {tr} | {wr} | {net} | {hw}/{hl} |".format(
                coin=coin,
                r=_n(c["range_pct"]), b=_n(c["bias_pct"]), tr=c["trades"],
                wr=_pct(c["win_rate"]), net=_n(c["net_pct_sum"]),
                hw=_n(c["avg_hold_win_h"]), hl=_n(c["avg_hold_loss_h"])))
        L.append("")

    # patterns
    pat = report["patterns"]
    L += ["## Patterns", ""]
    L.append(f"- **Volatility trend:** {pat['volatility_trend']} "
             f"({pat['volatility_by_week']})")
    tp = pat["trend_persistence"]
    L.append(f"- **Trend persistence:** {tp['continued']}/{tp['uptrend_followups']} "
             f"uptrend weeks continued (rate {_pct(tp['rate'])})")
    rot = pat["coin_rotation"]
    L.append(f"- **In vol-zone:** {', '.join(c['coin'] for c in rot['in_zone']) or '—'}")
    L.append(f"- **Entering zone:** {', '.join(rot['entering']) or '—'} | "
             f"**Leaving:** {', '.join(rot['leaving']) or '—'}")
    fee = pat.get("fee_efficiency")
    if fee:
        L.append(f"- **Fee efficiency:** {fee['cleared_pct']}% των trades κάλυψαν το fee "
                 f"({fee['cleared_fee']}/{fee['round_trips']})")
    L.append(f"- **corr(entry momentum, return):** "
             f"{_n(pat['entry_momentum_vs_return_corr'])}")
    if pat.get("winner_entry_hours"):
        L.append(f"- **Winner entry hours (UTC):** {pat['winner_entry_hours']}")
    if pat.get("loser_entry_hours"):
        L.append(f"- **Loser entry hours (UTC):** {pat['loser_entry_hours']}")
    L.append("")

    # what-if tables
    L += ["## Parameter What-If", ""]
    for param, rows in report["what_if"].items():
        L.append(f"### {param} (current: {CURRENT[param]})")
        if not rows:
            L.append("_no data_\n")
            continue
        keys = [k for k in rows[0].keys() if k not in ("value", "is_current")]
        L.append("| value | " + " | ".join(keys) + " |")
        L.append("|" + "---|" * (len(keys) + 1))
        for r in rows:
            mark = " ⬅︎ current" if r["is_current"] else ""
            L.append("| " + str(r["value"]) + mark + " | " +
                     " | ".join(str(r[k]) for k in keys) + " |")
        L.append("")

    # recommendations
    L += ["## Προτάσεις για την επόμενη εβδομάδα", ""]
    for k, v in report["recommendations"].items():
        if k == "_signal_warning":
            L.append(f"- ⚠️ {v}")
            continue
        tag = "🔧 ΑΛΛΑΓΗ" if v.get("change") else "✅ keep"
        L.append(f"- **{k}**: {tag} `{v['current']}` → `{v['suggested']}` "
                 f"[{v['confidence']}] — {v['rationale']}")
    L.append("")
    return "\n".join(L)


def _n(x):
    return "—" if x is None else f"{x:.2f}"


def _pct(x):
    return "—" if x is None else f"{x*100:.0f}"


# ===========================================================================
# Assembly
# ===========================================================================
def build_report(conn, weeks_limit):
    prices = load_prices(conn)
    trades = load_trades(conn)
    trips = reconstruct_round_trips(trades)

    weekly, weeks = weekly_coin_stats(prices, trips, weeks_limit)
    patterns = detect_patterns(prices, trips, weekly, weeks)
    what_if = parameter_what_if(prices, trips)
    recommendations, _conf = build_recommendations(what_if, trips, patterns)

    total_samples = sum(len(ts) for ts, _ in prices.values())
    wins = sum(1 for t in trips if t["is_winner"])
    losses = len(trips) - wins
    net = sum(t["net_pct"] for t in trips)

    # hot coins = latest week's coins ranked by range
    hot = []
    if weeks:
        last = weeks[-1]
        for coin, c in weekly[last].items():
            if c["range_pct"] is not None:
                hot.append({"coin": coin, "range_pct": c["range_pct"],
                            "bias_pct": c["bias_pct"]})
        hot.sort(key=lambda x: x["range_pct"], reverse=True)

    enough = len(trips) >= MIN_TRADES_FOR_STATS and total_samples >= MIN_PRICE_SAMPLES
    now = datetime.now(timezone.utc)

    report = {
        "generated_utc": now.isoformat(),
        "date": fmt_date(now),
        "strategy": "v2-pullback-in-uptrend",
        "status": "ok" if enough else "insufficient_data",
        "current_params": CURRENT,
        "weeks": weeks,
        "totals": {
            "round_trips": len(trips),
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round(wins / len(trips) * 100.0, 1) if trips else 0.0,
            "net_pct": round(net, 3),
            "price_samples": total_samples,
        },
        "weekly": weekly,
        "patterns": patterns,
        "what_if": what_if,
        "hot_coins": hot,
        "recommendations": recommendations,
    }
    return report


def write_artifacts(report):
    """Persist the machine-readable recommendations + full markdown report.

    Best-effort: a write failure must not crash the analysis run.
    """
    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        reco_doc = {
            "generated_utc": report["generated_utc"],
            "strategy": report["strategy"],
            "status": report["status"],
            "current_params": report["current_params"],
            "recommendations": report["recommendations"],
            # explicit: the live cycle must opt in before trusting these
            "apply": False,
            "note": "Advisory only. execute_kraken_cycle_v2.py does NOT read this yet.",
        }
        with open(RECO_PATH, "w", encoding="utf-8") as f:
            json.dump(reco_doc, f, ensure_ascii=False, indent=2, default=str)
        with open(REPORT_PATH, "w", encoding="utf-8") as f:
            f.write(render_markdown(report))
        return True
    except Exception as e:
        print(f"write_artifacts failed: {e}", file=sys.stderr)
        return False


def main():
    ap = argparse.ArgumentParser(description="AITrader v2 weekly strategy rethink")
    ap.add_argument("--format", choices=["telegram", "markdown", "json", "all"],
                    default="telegram", help="what to print to stdout")
    ap.add_argument("--weeks", type=int, default=0,
                    help="limit analysis to the last N ISO weeks (0 = all)")
    ap.add_argument("--no-write", action="store_true",
                    help="skip writing report artifacts to disk")
    args = ap.parse_args()

    conn = get_connection()
    if conn is None:
        print("📊 Εβδομαδιαία Ανασκόπηση AITrader v2\n\n"
              "❌ Αποτυχία σύνδεσης στη βάση. Δες τα logs.", file=sys.stdout)
        sys.exit(1)

    try:
        report = build_report(conn, args.weeks or None)
    finally:
        close_connection(conn)

    if not args.no_write:
        report["artifacts_written"] = write_artifacts(report)

    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    elif args.format == "markdown":
        print(render_markdown(report))
    elif args.format == "all":
        print(render_telegram(report))
        print("\n\n---\n\n")
        print(render_markdown(report))
        print("\n\n---\nJSON:\n")
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:  # telegram (default — cron forwards stdout here)
        print(render_telegram(report))


if __name__ == "__main__":
    main()
