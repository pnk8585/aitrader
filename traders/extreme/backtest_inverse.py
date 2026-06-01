"""
Inverse-hypothesis backtest for the crypto-momentum edge.

Reuses the SAME Kraken OHLC data + fee model as backtest.py (public Kraken REST,
no auth, stdlib only), but tests three *contrarian / grab-and-go* hypotheses
against the +2.0% momentum signal instead of the ride-the-wave live rules.

Strategies (all on the same 12-symbol universe, 0.26% per-side taker fee):

  1. SHORT-SPIKE  (short the pop, fade it back)
       ENTRY  : intraday change vs UTC-day open >= +2.0%  -> SHORT
       EXITS  : stop-loss  +3.5% (price moves against us)
                profit     -1.5% (price reverts in our favor)
                time-stop  2h
                no trailing

  2. LONG-DIP  (buy the dip, bounce play)
       ENTRY  : intraday change vs UTC-day open <= -2.0%  -> LONG
       EXITS  : stop-loss  -3.5%
                profit     +1.5%
                time-stop  2h
                no trailing

  3. FAST-SCALP  (same +2.0% long entry, grab-and-go)
       ENTRY  : intraday change vs UTC-day open >= +2.0%  -> LONG
       EXITS  : profit     +1.2% (fixed)
                stop-loss  -1.5%
                time-stop  1h
                no trailing, no breakeven, no profit-lock

One position at a time per symbol; re-entry allowed once flat. Entry fills on the
next bar's open + slippage (worsened directionally). PnL is measured per round trip
net of fees, reported as the % return *in the direction of the trade*.
"""
import sys
import json
import urllib.request
from datetime import datetime, timezone

# CCXT pair -> Kraken REST altname (identical universe to backtest.py)
PAIRS = {
    "BTC/EUR": "XBTEUR", "ETH/EUR": "ETHEUR", "SOL/EUR": "SOLEUR",
    "AVAX/EUR": "AVAXEUR", "LINK/EUR": "LINKEUR", "XRP/EUR": "XRPEUR",
    "DOGE/EUR": "XDGEUR", "SUI/EUR": "SUIEUR", "NEAR/EUR": "NEAREUR",
    "RENDER/EUR": "RENDEREUR", "ADA/EUR": "ADAEUR", "DOT/EUR": "DOTEUR",
}

INTERVAL = 15               # minutes per candle
PER_SIDE_FEE = 0.0026       # Kraken taker ~0.26% each side
ENTRY_SLIPPAGE = 0.001      # 0.1% market-order slippage on entry
ENTRY_PCT = 2.0             # |intraday move| trigger vs UTC-day open

FEE_RT = PER_SIDE_FEE * 100.0 * 2   # round-trip fee in pct points


# --- Strategy definitions -------------------------------------------------
# direction : +1 long, -1 short
# entry     : "above" (signal >= +ENTRY_PCT) or "below" (signal <= -ENTRY_PCT)
# target    : profit target, pct in trade direction (positive)
# stop      : stop-loss, pct in trade direction (negative)
# time_h    : time-stop in hours
STRATEGIES = [
    {"name": "SHORT-SPIKE", "direction": -1, "entry": "above",
     "target": 1.5, "stop": -3.5, "time_h": 2.0},
    {"name": "LONG-DIP",    "direction": +1, "entry": "below",
     "target": 1.5, "stop": -3.5, "time_h": 2.0},
    {"name": "FAST-SCALP",  "direction": +1, "entry": "above",
     "target": 1.2, "stop": -1.5, "time_h": 1.0},
]


def fetch(altname, bars_needed=2880):
    """Fetch up to bars_needed 15m candles, paginating since Kraken maxes at 720/call."""
    all_rows = []
    since = 0
    while len(all_rows) < bars_needed:
        url = (f"https://api.kraken.com/0/public/OHLC?pair={altname}"
               f"&interval={INTERVAL}&since={since}")
        req = urllib.request.Request(url, headers={"User-Agent": "backtest/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
        if data.get("error"):
            raise RuntimeError(", ".join(data["error"]))
        result = data["result"]
        key = next(k for k in result if k != "last")
        rows = result[key]
        if not rows:
            break
        # row: [time, open, high, low, close, vwap, volume, count]
        parsed = [[int(x[0]) * 1000, float(x[1]), float(x[2]), float(x[3]),
                   float(x[4]), float(x[6])] for x in rows]
        # Avoid infinite loop if last page repeats
        if since and parsed and parsed[0][0] == since * 1000:
            parsed = parsed[1:]
        if not parsed:
            break
        all_rows.extend(parsed)
        since = int(rows[-1][0])
        if len(rows) < 720:
            break
    return all_rows[-bars_needed:] if len(all_rows) > bars_needed else all_rows


def day_key(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def simulate(bars, strat):
    """
    Simulate one strategy on one symbol's bars.
    Returns list of realized round-trip net returns (%, after fees), expressed
    as profit in the trade's direction (positive = the trade made money).
    """
    # First bar's open per calendar day proxies the UTC-day open.
    day_open = {}
    for b in bars:
        d = day_key(b[0])
        if d not in day_open:
            day_open[d] = b[1]

    direction = strat["direction"]
    entry_mode = strat["entry"]
    target = strat["target"]
    stop = strat["stop"]
    time_h = strat["time_h"]
    bars_per_h = 60.0 / INTERVAL

    trades = []
    pos = None

    for i, b in enumerate(bars):
        ts, o, h, l, c, v = b
        d = day_key(ts)
        dopen = day_open.get(d)
        if not dopen or dopen <= 0:
            continue

        # --- Manage existing position ---
        if pos is not None:
            entry = pos["entry"]
            # Raw price moves vs entry (independent of direction)
            hi_pct = (h - entry) / entry * 100.0
            lo_pct = (l - entry) / entry * 100.0
            cl_pct = (c - entry) / entry * 100.0
            age_h = (i - pos["i0"]) / bars_per_h

            # Convert to trade-direction returns:
            #   long  favorable = price up; short favorable = price down.
            if direction > 0:
                fav_pct = hi_pct      # best move in our favor this bar
                adv_pct = lo_pct      # worst move against us this bar
                dir_close = cl_pct
            else:
                fav_pct = -lo_pct     # short profits when price falls
                adv_pct = -hi_pct
                dir_close = -cl_pct

            exit_ret = None
            # Stop checked first (conservative intrabar assumption)
            if adv_pct <= stop:
                exit_ret = stop
            elif fav_pct >= target:
                exit_ret = target
            elif age_h > time_h:
                exit_ret = dir_close

            if exit_ret is not None:
                trades.append(exit_ret - FEE_RT)
                pos = None
                continue

        # --- Evaluate entry (only when flat) ---
        if pos is None:
            signal_pct = (c - dopen) / dopen * 100.0
            trigger = (signal_pct >= ENTRY_PCT) if entry_mode == "above" \
                else (signal_pct <= -ENTRY_PCT)
            if trigger:
                # Fill on next bar open + slippage worsened in trade direction.
                base = bars[i + 1][1] if i + 1 < len(bars) else c
                if direction > 0:
                    entry_price = base * (1 + ENTRY_SLIPPAGE)   # buy higher
                else:
                    entry_price = base * (1 - ENTRY_SLIPPAGE)   # sell lower
                pos = {"entry": entry_price, "i0": i}

    # Close any open position at last bar
    if pos is not None:
        last = bars[-1]
        cl_pct = (last[4] - pos["entry"]) / pos["entry"] * 100.0
        dir_close = cl_pct if direction > 0 else -cl_pct
        trades.append(dir_close - FEE_RT)

    return trades


def stats(trades):
    """Return (n, wr, avg_w, avg_l, expectancy, total, eq50, be_wr)."""
    n = len(trades)
    if n == 0:
        return (0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0)
    wins = [x for x in trades if x > 0]
    losses = [x for x in trades if x <= 0]
    wr = len(wins) / n * 100
    avg_w = sum(wins) / len(wins) if wins else 0.0
    avg_l = sum(losses) / len(losses) if losses else 0.0
    total = sum(trades)
    eq = 1.0
    for x in trades:
        eq *= (1 + 0.5 * x / 100.0)        # 50% sizing proxy
    be_wr = (-avg_l) / (avg_w - avg_l) * 100 if (avg_w - avg_l) else 0.0
    return (n, wr, avg_w, avg_l, total / n, total, eq, be_wr)


def main():
    # Fetch all data once, shared across the three strategies.
    all_bars = {}
    for sym, alt in PAIRS.items():
        try:
            bars = fetch(alt)
            if len(bars) < 50:
                print(f"  {sym:11s} skipped ({len(bars)} bars)", file=sys.stderr)
                continue
            all_bars[sym] = bars
        except Exception as e:
            print(f"  {sym:11s} ERROR {e}", file=sys.stderr)

    if not all_bars:
        print("No data fetched.")
        return

    span_days = max((b[-1][0] - b[0][0]) / 86400000.0 for b in all_bars.values())
    print(f"Data: {INTERVAL}m candles, {len(all_bars)} pairs, "
          f"~{span_days:.0f}d, per-side fee {PER_SIDE_FEE*100:.2f}%")

    for strat in STRATEGIES:
        per_sym = {sym: simulate(bars, strat) for sym, bars in all_bars.items()}
        all_trades = [x for t in per_sym.values() for x in t]

        n, wr, avg_w, avg_l, exp, total, eq, be_wr = stats(all_trades)

        print(f"\n================ {strat['name']:^11s} "
              f"({'LONG' if strat['direction'] > 0 else 'SHORT'}, "
              f"target {strat['target']:+.1f}% / stop {strat['stop']:+.1f}% / "
              f"{strat['time_h']:.0f}h) ================")
        if n == 0:
            print("No trades triggered in the window.")
            continue
        wins = sum(1 for x in all_trades if x > 0)
        print(f"Total round-trip trades : {n}")
        print(f"Win rate                : {wr:.1f}%   ({wins}W / {n - wins}L)")
        print(f"Avg win                 : +{avg_w:.2f}%")
        print(f"Avg loss                : {avg_l:.2f}%")
        print(f"Expectancy / trade      : {exp:+.3f}%   (net of fees)")
        print(f"Sum of net returns      : {total:+.1f}%   (additive, 100% sizing)")
        print(f"Compounded equity (50%) : x{eq:.3f}   ({(eq-1)*100:+.1f}%)")
        print(f"Break-even win rate req. : {be_wr:.1f}%   -> edge is "
              f"{'POSITIVE' if wr > be_wr else 'NEGATIVE'}")

        # Per-symbol breakdown
        print("--- Per-symbol breakdown ---")
        for sym in sorted(per_sym.keys()):
            t = per_sym[sym]
            if not t:
                print(f"  {sym:11s}: no trades")
                continue
            sn, swr, saw, sal, sexp, stot, _, _ = stats(t)
            print(f"  {sym:11s}: trades={sn:3d}  WR={swr:5.1f}%  avgW={saw:+.2f}%  "
                  f"avgL={sal:+.2f}%  exp={sexp:+.3f}%  net={stot:+.1f}%")

    print("\n=========================================================================")


if __name__ == "__main__":
    main()
