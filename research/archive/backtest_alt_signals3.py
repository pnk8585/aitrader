"""
Test if the issue is the 1h hold. Try:
1. Very short hold (15m)
2. Very long hold (4h, EOD)
3. Only exit on stop or EOD (no time stop)
4. What if we only take signals that happen AFTER a pullback within the day?
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

# --- Test 1: 15m hold ---
trades_by_sym = {sym: [] for sym in syms}
for i in range(min_len):
    for sym in syms:
        b = all_bars[sym][i]
        ts, o, h, l, c, v = b
        d = day_key(ts)
        do = sym_day_open[sym].get(d)
        if not do:
            continue
        signal = (c - do) / do * 100.0
        if signal >= 2.0:
            if i + 1 >= len(all_bars[sym]):
                continue
            entry = all_bars[sym][i+1][1] * (1 + ENTRY_SLIPPAGE)
            exit_idx = min(i + 1 + 1, len(all_bars[sym]) - 1)
            exit_p = all_bars[sym][exit_idx][4]
            ret = (exit_p - entry) / entry * 100.0 - PER_SIDE_FEE * 100.0 * 2
            trades_by_sym[sym].append(ret)
all_t = []
for t in trades_by_sym.values():
    all_t += t
summarize(all_t, "HOLD 15m (+2% signal)")

# --- Test 2: 4h hold ---
trades_by_sym = {sym: [] for sym in syms}
for i in range(min_len):
    for sym in syms:
        b = all_bars[sym][i]
        ts, o, h, l, c, v = b
        d = day_key(ts)
        do = sym_day_open[sym].get(d)
        if not do:
            continue
        signal = (c - do) / do * 100.0
        if signal >= 2.0:
            if i + 1 >= len(all_bars[sym]):
                continue
            entry = all_bars[sym][i+1][1] * (1 + ENTRY_SLIPPAGE)
            exit_idx = min(i + 1 + 16, len(all_bars[sym]) - 1)
            exit_p = all_bars[sym][exit_idx][4]
            ret = (exit_p - entry) / entry * 100.0 - PER_SIDE_FEE * 100.0 * 2
            trades_by_sym[sym].append(ret)
all_t = []
for t in trades_by_sym.values():
    all_t += t
summarize(all_t, "HOLD 4h (+2% signal)")

# --- Test 3: EOD hold ---
trades_by_sym = {sym: [] for sym in syms}
for i in range(min_len):
    for sym in syms:
        b = all_bars[sym][i]
        ts, o, h, l, c, v = b
        d = day_key(ts)
        do = sym_day_open[sym].get(d)
        if not do:
            continue
        signal = (c - do) / do * 100.0
        if signal >= 2.0:
            if i + 1 >= len(all_bars[sym]):
                continue
            entry = all_bars[sym][i+1][1] * (1 + ENTRY_SLIPPAGE)
            # Find EOD
            exit_idx = i + 1
            for j in range(i + 1, len(all_bars[sym])):
                if day_key(all_bars[sym][j][0]) == d:
                    exit_idx = j
                else:
                    break
            exit_p = all_bars[sym][exit_idx][4]
            ret = (exit_p - entry) / entry * 100.0 - PER_SIDE_FEE * 100.0 * 2
            trades_by_sym[sym].append(ret)
all_t = []
for t in trades_by_sym.values():
    all_t += t
summarize(all_t, "HOLD TO EOD (+2% signal)")

# --- Test 4: Signal only if day already had a pullback ---
trades_by_sym = {sym: [] for sym in syms}
for i in range(min_len):
    for sym in syms:
        b = all_bars[sym][i]
        ts, o, h, l, c, v = b
        d = day_key(ts)
        do = sym_day_open[sym].get(d)
        if not do:
            continue
        signal = (c - do) / do * 100.0
        if signal >= 2.0:
            # Check if there was a pullback today (low was below open at some point)
            day_low = min(all_bars[sym][j][3] for j in range(max(0, i - 20), i + 1) if day_key(all_bars[sym][j][0]) == d)
            if day_low >= do * 0.995:  # no meaningful pullback (<0.5%)
                continue
            if i + 1 >= len(all_bars[sym]):
                continue
            entry = all_bars[sym][i+1][1] * (1 + ENTRY_SLIPPAGE)
            exit_idx = min(i + 1 + 4, len(all_bars[sym]) - 1)
            exit_p = all_bars[sym][exit_idx][4]
            ret = (exit_p - entry) / entry * 100.0 - PER_SIDE_FEE * 100.0 * 2
            trades_by_sym[sym].append(ret)
all_t = []
for t in trades_by_sym.values():
    all_t += t
summarize(all_t, "PULLBACK FILTER (+2% signal, day had <0.5% dip)")

# --- Test 5: Signal only if it's the FIRST +2% of the day ---
trades_by_sym = {sym: [] for sym in syms}
for i in range(min_len):
    for sym in syms:
        b = all_bars[sym][i]
        ts, o, h, l, c, v = b
        d = day_key(ts)
        do = sym_day_open[sym].get(d)
        if not do:
            continue
        signal = (c - do) / do * 100.0
        if signal >= 2.0:
            # Check if any earlier bar today was also >= +2%
            earlier_signal = False
            for j in range(max(0, i - 20), i):
                if day_key(all_bars[sym][j][0]) == d:
                    if (all_bars[sym][j][4] - do) / do * 100.0 >= 2.0:
                        earlier_signal = True
                        break
            if earlier_signal:
                continue
            if i + 1 >= len(all_bars[sym]):
                continue
            entry = all_bars[sym][i+1][1] * (1 + ENTRY_SLIPPAGE)
            exit_idx = min(i + 1 + 4, len(all_bars[sym]) - 1)
            exit_p = all_bars[sym][exit_idx][4]
            ret = (exit_p - entry) / entry * 100.0 - PER_SIDE_FEE * 100.0 * 2
            trades_by_sym[sym].append(ret)
all_t = []
for t in trades_by_sym.values():
    all_t += t
summarize(all_t, "FIRST-SIGNAL-ONLY (+2%, first of day)")
