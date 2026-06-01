"""
Test alternative entry signals:
1. Mean reversion (short after +2% spike)
2. Breakout from 4h range (not day-open)
3. Pullback entry (wait for pullback after signal)
4. Volume-confirmed momentum
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
MAX_POSITIONS = 5
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

def simulate_mean_reversion(all_bars):
    """Short after +2% spike from day open."""
    syms = list(all_bars.keys())
    sym_day_open = {}
    for sym, bars in all_bars.items():
        dopen = {}
        for b in bars:
            d = day_key(b[0])
            if d not in dopen:
                dopen[d] = b[1]
        sym_day_open[sym] = dopen

    trades_by_sym = {sym: [] for sym in syms}
    bars_per_h = 60.0 / INTERVAL
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
            if signal >= 2.0:
                # Simulate short at next bar open, hold 1h
                if i + 1 >= len(all_bars[sym]):
                    continue
                entry = all_bars[sym][i+1][1] * (1 + ENTRY_SLIPPAGE)
                exit_idx = min(i + 1 + 4, len(all_bars[sym]) - 1)
                exit_p = all_bars[sym][exit_idx][4]
                # Short return: (entry - exit) / entry
                ret = (entry - exit_p) / entry * 100.0 - PER_SIDE_FEE * 100.0 * 2
                trades_by_sym[sym].append(ret)

    all_trades = []
    for t in trades_by_sym.values():
        all_trades += t
    return all_trades, trades_by_sym

def simulate_breakout(all_bars, lookback_bars=16, entry_mult=1.0):
    """Enter when price breaks above highest high of last N bars + entry_mult * ATR."""
    syms = list(all_bars.keys())
    trades_by_sym = {sym: [] for sym in syms}
    min_len = min(len(b) for b in all_bars.values())

    for i in range(min_len):
        for sym in syms:
            if i < lookback_bars + 1:
                continue
            bars = all_bars[sym]
            b = bars[i]
            ts, o, h, l, c, v = b

            # Compute range and ATR over lookback
            hh = max(bars[j][2] for j in range(i - lookback_bars, i))
            atr = sum(bars[j][2] - bars[j][3] for j in range(i - lookback_bars, i)) / lookback_bars
            atr_pct = atr / c * 100.0 if c > 0 else 0

            # Entry: close > hh + mult * atr
            threshold = hh * (1 + entry_mult * atr_pct / 100)
            if c > threshold:
                if i + 1 >= len(bars):
                    continue
                entry = bars[i+1][1] * (1 + ENTRY_SLIPPAGE)
                # Hold 1h
                exit_idx = min(i + 1 + 4, len(bars) - 1)
                exit_p = bars[exit_idx][4]
                ret = (exit_p - entry) / entry * 100.0 - PER_SIDE_FEE * 100.0 * 2
                trades_by_sym[sym].append(ret)

    all_trades = []
    for t in trades_by_sym.values():
        all_trades += t
    return all_trades, trades_by_sym

def simulate_pullback(all_bars, pullback_pct=0.5):
    """Wait for pullback after +2% day-open signal, then enter."""
    syms = list(all_bars.keys())
    sym_day_open = {}
    for sym, bars in all_bars.items():
        dopen = {}
        for b in bars:
            d = day_key(b[0])
            if d not in dopen:
                dopen[d] = b[1]
        sym_day_open[sym] = dopen

    trades_by_sym = {sym: [] for sym in syms}
    min_len = min(len(b) for b in all_bars.values())

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
            if signal >= 2.0:
                # Look for pullback in next 2 bars
                for j in range(i + 1, min(i + 3, len(bars))):
                    pullback = (bars[j][4] - do) / do * 100.0
                    if pullback <= signal - pullback_pct:
                        # Enter at next bar after pullback
                        if j + 1 >= len(bars):
                            break
                        entry = bars[j+1][1] * (1 + ENTRY_SLIPPAGE)
                        exit_idx = min(j + 1 + 4, len(bars) - 1)
                        exit_p = bars[exit_idx][4]
                        ret = (exit_p - entry) / entry * 100.0 - PER_SIDE_FEE * 100.0 * 2
                        trades_by_sym[sym].append(ret)
                        break

    all_trades = []
    for t in trades_by_sym.values():
        all_trades += t
    return all_trades, trades_by_sym

def simulate_volume_confirmed(all_bars):
    """Only enter if signal bar volume > 2x avg of last 16 bars."""
    syms = list(all_bars.keys())
    sym_day_open = {}
    for sym, bars in all_bars.items():
        dopen = {}
        for b in bars:
            d = day_key(b[0])
            if d not in dopen:
                dopen[d] = b[1]
        sym_day_open[sym] = dopen

    trades_by_sym = {sym: [] for sym in syms}
    min_len = min(len(b) for b in all_bars.values())

    for i in range(min_len):
        for sym in syms:
            bars = all_bars[sym]
            b = bars[i]
            ts, o, h, l, c, v = b
            d = day_key(ts)
            do = sym_day_open[sym].get(d)
            if not do or i < 16:
                continue
            signal = (c - do) / do * 100.0
            if signal >= 2.0:
                avg_vol = sum(bars[j][5] for j in range(i - 16, i)) / 16
                if v < avg_vol * 2:
                    continue
                if i + 1 >= len(bars):
                    continue
                entry = bars[i+1][1] * (1 + ENTRY_SLIPPAGE)
                exit_idx = min(i + 1 + 4, len(bars) - 1)
                exit_p = bars[exit_idx][4]
                ret = (exit_p - entry) / entry * 100.0 - PER_SIDE_FEE * 100.0 * 2
                trades_by_sym[sym].append(ret)

    all_trades = []
    for t in trades_by_sym.values():
        all_trades += t
    return all_trades, trades_by_sym

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

# Fetch
print("Fetching data...")
all_bars = {}
for sym, alt in PAIRS.items():
    try:
        bars = fetch(alt, bars_needed=720)
        if len(bars) >= 50:
            all_bars[sym] = bars
    except Exception as e:
        print(f"  {sym} ERROR {e}")

print("\n" + "=" * 70)
print("ALTERNATIVE SIGNAL TESTS")
print("=" * 70)

# Baseline (original day-open momentum, 1h hold)
syms = list(all_bars.keys())
sym_day_open = {}
for sym, bars in all_bars.items():
    dopen = {}
    for b in bars:
        d = day_key(b[0])
        if d not in dopen:
            dopen[d] = b[1]
    sym_day_open[sym] = dopen

baseline_trades = {sym: [] for sym in syms}
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
        if signal >= 2.0:
            if i + 1 >= len(all_bars[sym]):
                continue
            entry = all_bars[sym][i+1][1] * (1 + ENTRY_SLIPPAGE)
            exit_idx = min(i + 1 + 4, len(all_bars[sym]) - 1)
            exit_p = all_bars[sym][exit_idx][4]
            ret = (exit_p - entry) / entry * 100.0 - PER_SIDE_FEE * 100.0 * 2
            baseline_trades[sym].append(ret)
all_b = []
for t in baseline_trades.values():
    all_b += t
summarize(all_b, "BASELINE (day-open +2%, hold 1h)")

# Mean reversion (short)
trades, _ = simulate_mean_reversion(all_bars)
summarize(trades, "MEAN REVERSION (short +2% spike, hold 1h)")

# Breakout from 4h range
trades, _ = simulate_breakout(all_bars, lookback_bars=16, entry_mult=0.5)
summarize(trades, "BREAKOUT 4h range (0.5x ATR)")

# Pullback entry
trades, _ = simulate_pullback(all_bars, pullback_pct=0.5)
summarize(trades, "PULLBACK (wait -0.5% from signal)")

# Volume confirmed
trades, _ = simulate_volume_confirmed(all_bars)
summarize(trades, "VOLUME CONFIRMED (>2x avg)")
