"""
The pullback filter shows promise (33.2% WR, -0.519% exp vs baseline -0.737%).
Let's refine it and test more sophisticated filters.
Also test: what if we enter on the pullback itself, not after the +2% signal?
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

# --- Refined pullback filter: vary the pullback depth ---
for pullback_thr in [0.3, 0.5, 0.8, 1.0, 1.5]:
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
                day_low = min(all_bars[sym][j][3] for j in range(max(0, i - 20), i + 1) if day_key(all_bars[sym][j][0]) == d)
                if day_low >= do * (1 - pullback_thr / 100):
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
    summarize(all_t, f"PULLBACK >={pullback_thr}% + hold 1h")

print()

# --- Enter on the dip, not the spike ---
# Wait for price to hit +2%, then pull back >= X%, then enter on bounce
for dip_pct in [0.5, 1.0, 1.5]:
    trades_by_sym = {sym: [] for sym in syms}
    for i in range(min_len):
        for sym in syms:
            b = all_bars[sym][i]
            ts, o, h, l, c, v = b
            d = day_key(ts)
            do = sym_day_open[sym].get(d)
            if not do:
                continue
            # Check if this bar is a pullback from a prior +2% signal
            # Find if there was a +2% signal in last 4 bars, and now we're down dip_pct from that peak
            signal_peak = None
            signal_idx = None
            for j in range(max(0, i - 4), i + 1):
                if (all_bars[sym][j][4] - do) / do * 100.0 >= 2.0:
                    signal_peak = all_bars[sym][j][4]
                    signal_idx = j
            if signal_peak is None:
                continue
            # Current close is down >= dip_pct from signal peak
            if (signal_peak - c) / signal_peak * 100.0 >= dip_pct:
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
    summarize(all_t, f"BOUNCE AFTER {dip_pct}% PULLBACK FROM +2%")

print()

# --- What about entering on strength continuation? ---
# Enter when price makes NEW high for the day (not just +2% from open)
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
            # Check if this is a new day high close
            day_high_close = max(all_bars[sym][j][4] for j in range(max(0, i - 20), i + 1) if day_key(all_bars[sym][j][0]) == d)
            if c < day_high_close * 0.999:
                continue  # not a new high
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
summarize(all_t, "NEW DAY HIGH (+2% from open, new high close)")
