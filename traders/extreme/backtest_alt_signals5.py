"""
The pullback filter improves things but still negative.
Let's try the most promising direction: combine pullback with proper exits.
Also test: what if the strategy is fundamentally sound but the time-stop is the killer?
Test with NO time-stop, only stop-loss and trailing profit.
"""
import json, urllib.request
from datetime import datetime, timezone

PAIRS = {
    "BTC/EUR": "XBTEUR", "ETH/EUR": "ETHEUR", "SOL/EUR": "SOLEUR",
    "AVAX/EUR": "AVAXEUR", "LINK/EUR": "LINKEUR", "XRP/EUR": "XRPEUR",
    "DOGE/EUR": "XDGEUR", "SUI/EUR": "SUIEUR", "NEAR/EUR": "NEAREUR",
    "RENDER/EUR": "RENDEREUR", "ADA/EUR": "ADAEUR", "DOT/EUR": "DOTEUR",
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

# --- Full simulation with NO time-stop, pullback filter, proper exits ---
def simulate_full(all_bars, use_pullback=False, pullback_thr=0.5,
                  stop_pct=-3.5, ttp_peak=3.0, ttp_gb=1.0,
                  plock_peak=5.0, plock_drop=3.0,
                  be_peak=1.0, be_floor=0.6,
                  time_stop_h=999, max_hold_h=4):
    syms = list(all_bars.keys())
    trades_by_sym = {sym: [] for sym in syms}
    bars_per_h = 60.0 / INTERVAL

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

            # Pullback filter
            if use_pullback:
                day_low = min(bars[j][3] for j in range(max(0, i - 20), i + 1) if day_key(bars[j][0]) == d)
                if day_low >= do * (1 - pullback_thr / 100):
                    continue

            if i + 1 >= len(bars):
                continue
            entry = bars[i+1][1] * (1 + ENTRY_SLIPPAGE)

            # Manage position
            peak = 0.0
            exited = False
            max_bars = int(max_hold_h * bars_per_h)
            for j in range(i + 1, min(i + 1 + max_bars, len(bars))):
                jb = bars[j]
                hi_pct = (jb[2] - entry) / entry * 100.0
                lo_pct = (jb[3] - entry) / entry * 100.0
                cl_pct = (jb[4] - entry) / entry * 100.0
                peak = max(peak, hi_pct)
                age_h = (j - (i + 1)) / bars_per_h

                exit_pct = None
                if lo_pct <= stop_pct:
                    exit_pct = stop_pct
                elif peak >= ttp_peak and cl_pct <= (peak - ttp_gb):
                    exit_pct = cl_pct
                elif peak >= plock_peak and cl_pct < plock_drop:
                    exit_pct = cl_pct
                elif peak >= be_peak and cl_pct <= be_floor:
                    exit_pct = cl_pct
                elif age_h > time_stop_h:
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
    return all_t, trades_by_sym

# Baseline
all_t, _ = simulate_full(all_bars, use_pullback=False, time_stop_h=1.0, max_hold_h=4)
summarize(all_t, "BASELINE (no pullback, time-stop 1h, max hold 4h)")

# No time stop, 4h max
all_t, _ = simulate_full(all_bars, use_pullback=False, time_stop_h=999, max_hold_h=4)
summarize(all_t, "NO TIME-STOP (max hold 4h)")

# Pullback 0.5%, no time stop
all_t, _ = simulate_full(all_bars, use_pullback=True, pullback_thr=0.5, time_stop_h=999, max_hold_h=4)
summarize(all_t, "PULLBACK 0.5% + NO TIME-STOP (max 4h)")

# Pullback 1.0%, no time stop
all_t, ts = simulate_full(all_bars, use_pullback=True, pullback_thr=1.0, time_stop_h=999, max_hold_h=4)
summarize(all_t, "PULLBACK 1.0% + NO TIME-STOP (max 4h)")
for sym in sorted(ts.keys()):
    if ts[sym]:
        w = [x for x in ts[sym] if x > 0]
        wr = len(w) / len(ts[sym]) * 100
        print(f"    {sym}: trades={len(ts[sym])} WR={wr:.1f}% net={sum(ts[sym]):+.1f}%")

# Wider stops, pullback 1%
all_t, _ = simulate_full(all_bars, use_pullback=True, pullback_thr=1.0,
                         stop_pct=-5.0, ttp_peak=4.0, ttp_gb=1.5,
                         plock_peak=7.0, plock_drop=4.0,
                         time_stop_h=999, max_hold_h=6)
summarize(all_t, "PULLBACK 1% + WIDER EXITS (stop -5%, trail 4%/1.5%, max 6h)")

# Very tight: scalp the pullback
all_t, _ = simulate_full(all_bars, use_pullback=True, pullback_thr=0.5,
                         stop_pct=-2.0, ttp_peak=2.0, ttp_gb=0.5,
                         plock_peak=3.0, plock_drop=2.0,
                         be_peak=0.5, be_floor=0.3,
                         time_stop_h=0.5, max_hold_h=2)
summarize(all_t, "SCALP PULLBACK (stop -2%, trail 2%/0.5%, time 0.5h)")
