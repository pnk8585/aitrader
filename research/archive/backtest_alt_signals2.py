"""
More alternative tests:
1. Mean reversion short with proper exits (not just 1h hold)
2. Buy the dip (enter on -2% from day open)
3. Range breakout with volume
4. Only trade first 4h of day (UTC 00:00-04:00) when ranges establish
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

def hour_of_day(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).hour

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

# --- Test 1: Buy the dip (-2% from day open) ---
trades_by_sym = {sym: [] for sym in syms}
min_len = min(len(b) for b in all_bars.values())
for i in range(min_len):
    for sym in syms:
        b = all_bars[sym][i]
        ts, o, h, l, c, v = b
        d = day_key(ts)
        do = sym_day_open[sym].get(d)
        if not do:
            continue
        signal = (c - do) / do * 100.0
        if signal <= -2.0:
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
summarize(all_t, "BUY THE DIP (-2% from day open, hold 1h)")

# --- Test 2: Only trade first 4h UTC ---
trades_by_sym = {sym: [] for sym in syms}
for i in range(min_len):
    for sym in syms:
        b = all_bars[sym][i]
        ts, o, h, l, c, v = b
        if hour_of_day(ts) >= 4:
            continue
        d = day_key(ts)
        do = sym_day_open[sym].get(d)
        if not do:
            continue
        signal = (c - do) / do * 100.0
        if signal >= 2.0:
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
summarize(all_t, "FIRST 4H ONLY (+2% signal, UTC 00-04)")

# --- Test 3: Mean reversion short with trailing stop ---
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
            # Short at next bar open
            if i + 1 >= len(all_bars[sym]):
                continue
            entry = all_bars[sym][i+1][1] * (1 + ENTRY_SLIPPAGE)
            # Manage short position
            peak_profit = 0.0  # most negative (best for short)
            exited = False
            for j in range(i + 1, min(i + 1 + 16, len(all_bars[sym]))):  # max 4h
                jb = all_bars[sym][j]
                ret_pct = (entry - jb[4]) / entry * 100.0  # short P/L
                hi_pct = (entry - jb[3]) / entry * 100.0   # best case (price went down)
                lo_pct = (entry - jb[2]) / entry * 100.0   # worst case (price went up)
                peak_profit = max(peak_profit, hi_pct)

                # Stop loss for short: -3.5% (price went up 3.5%)
                if lo_pct <= -3.5:
                    trades_by_sym[sym].append(-3.5 - PER_SIDE_FEE * 100.0 * 2)
                    exited = True
                    break
                # Trailing profit: if peak_profit >= 2%, exit if retraces 1%
                if peak_profit >= 2.0:
                    current_from_peak = peak_profit - ret_pct
                    if current_from_peak >= 1.0:
                        trades_by_sym[sym].append(ret_pct - PER_SIDE_FEE * 100.0 * 2)
                        exited = True
                        break
                # Time stop
                if j >= i + 1 + 4:
                    trades_by_sym[sym].append(ret_pct - PER_SIDE_FEE * 100.0 * 2)
                    exited = True
                    break
            if not exited:
                jb = all_bars[sym][min(i + 1 + 16, len(all_bars[sym]) - 1)]
                ret_pct = (entry - jb[4]) / entry * 100.0
                trades_by_sym[sym].append(ret_pct - PER_SIDE_FEE * 100.0 * 2)
all_t = []
for t in trades_by_sym.values():
    all_t += t
summarize(all_t, "SHORT WITH EXITS (trail 2%/1%, stop -3.5%, 4h max)")

# --- Test 4: Range expansion breakout ---
trades_by_sym = {sym: [] for sym in syms}
for i in range(min_len):
    for sym in syms:
        if i < 32:
            continue
        bars = all_bars[sym]
        b = bars[i]
        ts, o, h, l, c, v = b
        # 8h range
        hh = max(bars[j][2] for j in range(i - 32, i))
        ll = min(bars[j][3] for j in range(i - 32, i))
        range_pct = (hh - ll) / c * 100.0 if c > 0 else 0
        # Breakout above 8h high + 0.3 * range
        threshold = hh + 0.3 * (hh - ll)
        if c > threshold and range_pct > 2.0:
            if i + 1 >= len(bars):
                continue
            entry = bars[i+1][1] * (1 + ENTRY_SLIPPAGE)
            exit_idx = min(i + 1 + 4, len(bars) - 1)
            exit_p = bars[exit_idx][4]
            ret = (exit_p - entry) / entry * 100.0 - PER_SIDE_FEE * 100.0 * 2
            trades_by_sym[sym].append(ret)
all_t = []
for t in trades_by_sym.values():
    all_t += t
summarize(all_t, "8h RANGE BREAKOUT (0.3x range expansion)")
