"""
NEAR/RENDER only + pullback gets to -0.123% exp, 47.4% WR.
Very close to breakeven. Let's push it over the edge with:
1. No slippage assumption (limit orders)
2. Tighter stop for these volatile coins
3. Higher profit targets
4. Only take signals with volume spike
5. What about 2h hold instead of 4h?
"""
import json, urllib.request
from datetime import datetime, timezone

PAIRS = {
    "NEAR/EUR": "NEAREUR", "RENDER/EUR": "RENDEREUR",
}
INTERVAL = 15
PER_SIDE_FEE = 0.0026
ENTRY_SLIPPAGE = 0.001

def fetch(altname, bars_needed=2880):
    all_rows = []
    since = 0
    while len(all_rows) < bars_needed:
        url = f"https://api.kraken.com/0/public/OHLC?pair={altname}&interval={INTERVAL}&since={since}"
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
        parsed = [[int(x[0]) * 1000, float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[6])] for x in rows]
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

def summarize(all_trades, label):
    if not all_trades:
        print(f"{label}: NO TRADES")
        return
    n = len(all_trades)
    wins = [x for x in all_trades if x > 0]
    losses = [x for x in all_trades if x <= 0]
    wr = len(wins) / n * 100
    avg_w = sum(wins) / len(wins) if wins else 0.0
    avg_l = sum(losses) / len(losses) if losses else 0.0
    total = sum(all_trades)
    eq = 1.0
    for x in all_trades:
        eq *= (1 + 0.5 * x / 100.0)
    be_wr = (-avg_l) / (avg_w - avg_l) * 100 if (avg_w - avg_l) else 0.0
    print(f"{label}")
    print(f"  Trades={n}  WR={wr:.1f}%  AvgW=+{avg_w:.2f}%  AvgL={avg_l:.2f}%  Exp={total/n:+.3f}%  Eq={(eq-1)*100:+.1f}%  BEreq={be_wr:.1f}%")

print("Fetching data...")
all_bars = {}
for sym, alt in PAIRS.items():
    try:
        bars = fetch(alt, bars_needed=720)
        if len(bars) >= 50:
            all_bars[sym] = bars
    except Exception as e:
        print(f"  {sym} ERROR {e}")

syms = list(all_bars.keys())
sym_day_open = {}
for sym, bars in all_bars.items():
    dopen = {}
    for b in bars:
        d = day_key(b[0])
        if d not in dopen:
            dopen[d] = b[1]
    sym_day_open[sym] = dopen

min_len = min(len(b) for b in all_bars.values())

def run_test(label, entry_slip, stop_pct, ttp_peak, ttp_gb, plock_peak, plock_drop, be_peak, be_floor, max_hold_h, pullback_thr, use_volume=False, vol_mult=2.0):
    trades_by_sym = {sym: [] for sym in syms}
    for i in range(min_len):
        for sym in syms:
            bars = all_bars[sym]
            b = bars[i]
            ts, o, h, l, c, v = b
            d = day_key(ts)
            do = sym_day_open[sym].get(d)
            if not do:
                continue
            signal = (c - do) / do * 100.0
            if signal < 2.0:
                continue
            day_low = min(bars[j][3] for j in range(max(0, i - 20), i + 1) if day_key(bars[j][0]) == d)
            if day_low >= do * (1 - pullback_thr / 100):
                continue
            if use_volume and i >= 16:
                avg_vol = sum(bars[j][5] for j in range(i - 16, i)) / 16
                if v < avg_vol * vol_mult:
                    continue
            if i + 1 >= len(bars):
                continue
            entry = bars[i+1][1] * (1 + entry_slip)
            peak = 0.0
            exited = False
            max_bars = int(max_hold_h * 60.0 / INTERVAL)
            for j in range(i + 1, min(i + 1 + max_bars, len(bars))):
                jb = bars[j]
                hi_pct = (jb[2] - entry) / entry * 100.0
                lo_pct = (jb[3] - entry) / entry * 100.0
                cl_pct = (jb[4] - entry) / entry * 100.0
                peak = max(peak, hi_pct)
                exit_pct = None
                if lo_pct <= stop_pct:
                    exit_pct = stop_pct
                elif peak >= ttp_peak and cl_pct <= (peak - ttp_gb):
                    exit_pct = cl_pct
                elif peak >= plock_peak and cl_pct < plock_drop:
                    exit_pct = cl_pct
                elif peak >= be_peak and cl_pct <= be_floor:
                    exit_pct = cl_pct
                elif j >= i + 1 + max_bars - 1:
                    exit_pct = cl_pct
                if exit_pct is not None:
                    net = exit_pct - PER_SIDE_FEE * 100.0 * 2
                    trades_by_sym[sym].append(net)
                    exited = True
                    break
            if not exited:
                jb = bars[min(i + 1 + max_bars - 1, len(bars) - 1)]
                cl_pct = (jb[4] - entry) / entry * 100.0
                net = cl_pct - PER_SIDE_FEE * 100.0 * 2
                trades_by_sym[sym].append(net)
    all_t = []
    for t in trades_by_sym.values():
        all_t += t
    summarize(all_t, label)
    for sym in sorted(trades_by_sym.keys()):
        if trades_by_sym[sym]:
            w = [x for x in trades_by_sym[sym] if x > 0]
            wr = len(w) / len(trades_by_sym[sym]) * 100
            print(f"    {sym}: trades={len(trades_by_sym[sym])} WR={wr:.1f}% net={sum(trades_by_sym[sym]):+.1f}%")

print("=" * 70)
print("NEAR/RENDER OPTIMIZATION")
print("=" * 70)

# Baseline for NEAR/RENDER
run_test("BASELINE (slip 0.1%, stop -3.5, trail 3/1, max 4h)",
         0.001, -3.5, 3.0, 1.0, 5.0, 3.0, 1.0, 0.6, 4, 1.0)

# No slippage (limit order)
run_test("NO SLIPPAGE (limit entry)",
         0.0, -3.5, 3.0, 1.0, 5.0, 3.0, 1.0, 0.6, 4, 1.0)

# Tighter stop
run_test("TIGHT STOP -2.5%",
         0.001, -2.5, 3.0, 1.0, 5.0, 3.0, 1.0, 0.6, 4, 1.0)

# Wider targets
run_test("WIDE TARGETS (trail 5/2, plock 8/5)",
         0.001, -3.5, 5.0, 2.0, 8.0, 5.0, 1.0, 0.6, 4, 1.0)

# 2h hold
run_test("2H HOLD",
         0.001, -3.5, 3.0, 1.0, 5.0, 3.0, 1.0, 0.6, 2, 1.0)

# Volume confirmed
run_test("VOLUME >2x",
         0.001, -3.5, 3.0, 1.0, 5.0, 3.0, 1.0, 0.6, 4, 1.0, use_volume=True, vol_mult=2.0)

# Best combo: no slippage + wide targets + 2h
run_test("COMBO: no slip + wide targets + 2h",
         0.0, -3.5, 5.0, 2.0, 8.0, 5.0, 1.0, 0.6, 2, 1.0)

# Even wider
run_test("COMBO2: no slip + trail 6/2 + max 6h",
         0.0, -5.0, 6.0, 2.0, 10.0, 6.0, 1.5, 0.8, 6, 1.0)
