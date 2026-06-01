"""
The best so far: PULLBACK 1% + NO TIME-STOP gets to -0.271% exp (from -0.775% baseline).
Still negative but much closer. Let's try:
1. Only trade NEAR/RENDER with pullback filter (they're the only ones with any edge)
2. Combine pullback with volume filter
3. Test if the issue is entry timing - what if we enter at the signal bar close instead of next open?
4. Test different pullback thresholds per volatility tier
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

# --- Test 1: Only NEAR/RENDER with pullback ---
trades_by_sym = {sym: [] for sym in syms}
for i in range(min_len):
    for sym in ['NEAR/EUR', 'RENDER/EUR']:
        if sym not in all_bars:
            continue
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
        if day_low >= do * 0.99:
            continue
        if i + 1 >= len(bars):
            continue
        entry = bars[i+1][1] * (1 + ENTRY_SLIPPAGE)
        peak = 0.0
        exited = False
        for j in range(i + 1, min(i + 1 + 16, len(bars))):
            jb = bars[j]
            hi_pct = (jb[2] - entry) / entry * 100.0
            lo_pct = (jb[3] - entry) / entry * 100.0
            cl_pct = (jb[4] - entry) / entry * 100.0
            peak = max(peak, hi_pct)
            exit_pct = None
            if lo_pct <= -5.0:
                exit_pct = -5.0
            elif peak >= 4.0 and cl_pct <= (peak - 1.5):
                exit_pct = cl_pct
            elif peak >= 7.0 and cl_pct < 4.0:
                exit_pct = cl_pct
            elif peak >= 1.5 and cl_pct <= 0.8:
                exit_pct = cl_pct
            elif j >= i + 1 + 16 - 1:
                exit_pct = cl_pct
            if exit_pct is not None:
                net = exit_pct - PER_SIDE_FEE * 100.0 * 2
                trades_by_sym[sym].append(net)
                exited = True
                break
        if not exited:
            jb = bars[min(i + 1 + 16 - 1, len(bars) - 1)]
            cl_pct = (jb[4] - entry) / entry * 100.0
            net = cl_pct - PER_SIDE_FEE * 100.0 * 2
            trades_by_sym[sym].append(net)
all_t = []
for t in trades_by_sym.values():
    all_t += t
summarize(all_t, "NEAR/RENDER ONLY + pullback 1% + wider exits")

# --- Test 2: Entry at signal bar close (no slippage on next bar) ---
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
        if day_low >= do * 0.99:
            continue
        entry = c  # enter at signal bar close
        peak = 0.0
        exited = False
        for j in range(i + 1, min(i + 1 + 16, len(bars))):
            jb = bars[j]
            hi_pct = (jb[2] - entry) / entry * 100.0
            lo_pct = (jb[3] - entry) / entry * 100.0
            cl_pct = (jb[4] - entry) / entry * 100.0
            peak = max(peak, hi_pct)
            exit_pct = None
            if lo_pct <= -5.0:
                exit_pct = -5.0
            elif peak >= 4.0 and cl_pct <= (peak - 1.5):
                exit_pct = cl_pct
            elif peak >= 7.0 and cl_pct < 4.0:
                exit_pct = cl_pct
            elif peak >= 1.5 and cl_pct <= 0.8:
                exit_pct = cl_pct
            elif j >= i + 1 + 16 - 1:
                exit_pct = cl_pct
            if exit_pct is not None:
                net = exit_pct - PER_SIDE_FEE * 100.0 * 2
                trades_by_sym[sym].append(net)
                exited = True
                break
        if not exited:
            jb = bars[min(i + 1 + 16 - 1, len(bars) - 1)]
            cl_pct = (jb[4] - entry) / entry * 100.0
            net = cl_pct - PER_SIDE_FEE * 100.0 * 2
            trades_by_sym[sym].append(net)
all_t = []
for t in trades_by_sym.values():
    all_t += t
summarize(all_t, "ENTRY AT CLOSE (no next-bar slippage) + pullback 1%")

# --- Test 3: Per-tier pullback thresholds ---
tier_pullback = {
    'BTC/EUR': 0.3, 'ETH/EUR': 0.3, 'XRP/EUR': 0.3,
    'SOL/EUR': 0.5, 'AVAX/EUR': 0.5, 'LINK/EUR': 0.5,
    'DOGE/EUR': 0.5, 'ADA/EUR': 0.5, 'DOT/EUR': 0.5, 'SUI/EUR': 0.5,
    'NEAR/EUR': 1.5, 'RENDER/EUR': 1.5,
}
tier_entry = {
    'BTC/EUR': 1.2, 'ETH/EUR': 1.2, 'XRP/EUR': 1.5,
    'SOL/EUR': 1.8, 'AVAX/EUR': 1.8, 'LINK/EUR': 1.8,
    'DOGE/EUR': 1.8, 'ADA/EUR': 1.8, 'DOT/EUR': 1.8, 'SUI/EUR': 1.8,
    'NEAR/EUR': 3.0, 'RENDER/EUR': 3.0,
}
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
        entry_thr = tier_entry.get(sym, 2.0)
        if signal < entry_thr:
            continue
        pb_thr = tier_pullback.get(sym, 0.5)
        day_low = min(bars[j][3] for j in range(max(0, i - 20), i + 1) if day_key(bars[j][0]) == d)
        if day_low >= do * (1 - pb_thr / 100):
            continue
        if i + 1 >= len(bars):
            continue
        entry = bars[i+1][1] * (1 + ENTRY_SLIPPAGE)
        peak = 0.0
        exited = False
        for j in range(i + 1, min(i + 1 + 16, len(bars))):
            jb = bars[j]
            hi_pct = (jb[2] - entry) / entry * 100.0
            lo_pct = (jb[3] - entry) / entry * 100.0
            cl_pct = (jb[4] - entry) / entry * 100.0
            peak = max(peak, hi_pct)
            exit_pct = None
            if lo_pct <= -5.0:
                exit_pct = -5.0
            elif peak >= 4.0 and cl_pct <= (peak - 1.5):
                exit_pct = cl_pct
            elif peak >= 7.0 and cl_pct < 4.0:
                exit_pct = cl_pct
            elif peak >= 1.5 and cl_pct <= 0.8:
                exit_pct = cl_pct
            elif j >= i + 1 + 16 - 1:
                exit_pct = cl_pct
            if exit_pct is not None:
                net = exit_pct - PER_SIDE_FEE * 100.0 * 2
                trades_by_sym[sym].append(net)
                exited = True
                break
        if not exited:
            jb = bars[min(i + 1 + 16 - 1, len(bars) - 1)]
            cl_pct = (jb[4] - entry) / entry * 100.0
            net = cl_pct - PER_SIDE_FEE * 100.0 * 2
            trades_by_sym[sym].append(net)
all_t = []
for t in trades_by_sym.values():
    all_t += t
summarize(all_t, "TIERED (entry + pullback per tier)")

# --- Test 4: What if we only short? ---
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
        if i + 1 >= len(bars):
            continue
        entry = bars[i+1][1] * (1 + ENTRY_SLIPPAGE)
        # Short
        peak = 0.0  # best profit for short (price went down)
        exited = False
        for j in range(i + 1, min(i + 1 + 16, len(bars))):
            jb = bars[j]
            hi_pct = (entry - jb[3]) / entry * 100.0   # best case
            lo_pct = (entry - jb[2]) / entry * 100.0   # worst case
            cl_pct = (entry - jb[4]) / entry * 100.0
            peak = max(peak, hi_pct)
            exit_pct = None
            if lo_pct <= -3.5:  # stop loss for short
                exit_pct = -3.5
            elif peak >= 2.0 and cl_pct <= (peak - 1.0):
                exit_pct = cl_pct
            elif j >= i + 1 + 16 - 1:
                exit_pct = cl_pct
            if exit_pct is not None:
                net = exit_pct - PER_SIDE_FEE * 100.0 * 2
                trades_by_sym[sym].append(net)
                exited = True
                break
        if not exited:
            jb = bars[min(i + 1 + 16 - 1, len(bars) - 1)]
            cl_pct = (entry - jb[4]) / entry * 100.0
            net = cl_pct - PER_SIDE_FEE * 100.0 * 2
            trades_by_sym[sym].append(net)
all_t = []
for t in trades_by_sym.values():
    all_t += t
summarize(all_t, "SHORT ONLY (+2% signal, trail 2%/1%, stop -3.5%)")
