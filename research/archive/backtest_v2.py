"""
Backtest v2: tests alternative parameter sets for crypto momentum.
"""
import sys, json, urllib.request
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

def run_backtest(all_bars, params):
    syms = list(all_bars.keys())
    sym_day_open = {}
    for sym, bars in all_bars.items():
        dopen = {}
        for b in bars:
            d = day_key(b[0])
            if d not in dopen:
                dopen[d] = b[1]
        sym_day_open[sym] = dopen

    entered_today = {sym: set() for sym in syms}
    positions = {}
    trades_by_sym = {sym: [] for sym in syms}
    bars_per_h = 60.0 / INTERVAL

    min_len = min(len(b) for b in all_bars.values())

    for i in range(min_len):
        # Manage exits
        for sym in syms:
            if sym not in positions:
                continue
            pos = positions[sym]
            b = all_bars[sym][i]
            ts, o, h, l, c, v = b
            entry = pos["entry"]
            hi_pct = (h - entry) / entry * 100.0
            lo_pct = (l - entry) / entry * 100.0
            cl_pct = (c - entry) / entry * 100.0
            pos["peak"] = max(pos["peak"], hi_pct)
            peak = pos["peak"]
            age_h = (i - pos["i0"]) / bars_per_h
            pl = cl_pct

            exit_pct = None
            if lo_pct <= params['STOP_PCT']:
                exit_pct = params['STOP_PCT']
            elif peak >= params['TTP_PEAK'] and cl_pct <= (peak - params['TTP_GIVEBACK']):
                exit_pct = cl_pct
            elif peak >= params['PLOCK_PEAK'] and cl_pct < params['PLOCK_DROP']:
                exit_pct = cl_pct
            elif peak >= params['BE_PEAK'] and cl_pct <= params['BE_FLOOR']:
                exit_pct = cl_pct
            elif age_h > params['TIME_STOP_H']:
                exit_pct = cl_pct

            if exit_pct is not None:
                net = exit_pct - PER_SIDE_FEE * 100.0 * 2
                trades_by_sym[sym].append(net)
                del positions[sym]
                d = day_key(ts)
                entered_today[sym].discard(d)
                continue

            pos["stale"] = (age_h > (params['STALE_MIN'] / 60.0)) and (pl < params['STALE_PL_PCT'])
            pos["pl"] = pl

        # Compute signals
        signals = []
        for sym in syms:
            b = all_bars[sym][i]
            ts, o, h, l, c, v = b
            d = day_key(ts)
            dopen = sym_day_open[sym].get(d)
            if not dopen or dopen <= 0:
                continue
            signal_pct = (c - dopen) / dopen * 100.0
            # Per-symbol entry threshold
            entry_thr = params['ENTRY_PCT'].get(sym, params['ENTRY_PCT_DEFAULT'])
            signals.append((sym, signal_pct, d, i, entry_thr))

        # Stale rotation
        stale_syms = [s for s, p in positions.items() if p.get("stale")]
        if stale_syms:
            available = [s for s in signals if s[0] not in positions and s[1] >= params['ROTATION_ENTRY_PCT']]
            available.sort(key=lambda x: x[1], reverse=True)
            for stale_sym in stale_syms:
                if not available:
                    break
                best = available[0]
                pos = positions[stale_sym]
                b = all_bars[stale_sym][i]
                cl_pct = (b[4] - pos["entry"]) / pos["entry"] * 100.0
                net = cl_pct - PER_SIDE_FEE * 100.0 * 2
                trades_by_sym[stale_sym].append(net)
                del positions[stale_sym]
                d = day_key(b[0])
                entered_today[stale_sym].discard(d)
                if best[3] + 1 < len(all_bars[best[0]]):
                    next_open = all_bars[best[0]][best[3] + 1][1]
                    entry_price = next_open * (1 + ENTRY_SLIPPAGE)
                else:
                    entry_price = all_bars[best[0]][best[3]][4] * (1 + ENTRY_SLIPPAGE)
                positions[best[0]] = {"entry": entry_price, "peak": 0.0, "i0": i, "stale": False, "pl": 0.0}
                entered_today[best[0]].add(best[2])
                available = [s for s in available if s[0] != best[0]]

        # New entries
        for sym, signal_pct, d, idx, entry_thr in signals:
            if sym in positions:
                continue
            if d in entered_today[sym]:
                continue
            if signal_pct < entry_thr:
                continue
            if len(positions) >= MAX_POSITIONS:
                continue
            if idx + 1 < len(all_bars[sym]):
                next_open = all_bars[sym][idx + 1][1]
                entry_price = next_open * (1 + ENTRY_SLIPPAGE)
            else:
                entry_price = all_bars[sym][idx][4] * (1 + ENTRY_SLIPPAGE)
            positions[sym] = {"entry": entry_price, "peak": 0.0, "i0": i, "stale": False, "pl": 0.0}
            entered_today[sym].add(d)

    # Close remaining
    for sym, pos in list(positions.items()):
        last = all_bars[sym][-1]
        cl_pct = (last[4] - pos["entry"]) / pos["entry"] * 100.0
        net = cl_pct - PER_SIDE_FEE * 100.0 * 2
        trades_by_sym[sym].append(net)
        del positions[sym]

    all_trades = []
    for t in trades_by_sym.values():
        all_trades += t
    return all_trades, trades_by_sym

def summarize(all_trades, trades_by_sym, label):
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

# Fetch data
print("Fetching data...")
all_bars = {}
for sym, alt in PAIRS.items():
    try:
        bars = fetch(alt, bars_needed=720)
        if len(bars) >= 50:
            all_bars[sym] = bars
    except Exception as e:
        print(f"  {sym} ERROR {e}")

# --- Test multiple configs ---
configs = []

# 1. Current live rules
configs.append(("CURRENT", {
    'ENTRY_PCT_DEFAULT': 2.0,
    'ENTRY_PCT': {},
    'TTP_PEAK': 3.0, 'TTP_GIVEBACK': 1.0,
    'PLOCK_PEAK': 5.0, 'PLOCK_DROP': 3.0,
    'STOP_PCT': -3.5,
    'BE_PEAK': 1.0, 'BE_FLOOR': 0.6,
    'TIME_STOP_H': 1.0,
    'STALE_MIN': 30.0, 'STALE_PL_PCT': 1.0,
    'ROTATION_ENTRY_PCT': 2.5,
}))

# 2. Tiered entry thresholds
configs.append(("TIERED-v1", {
    'ENTRY_PCT_DEFAULT': 2.0,
    'ENTRY_PCT': {
        'BTC/EUR': 1.2, 'ETH/EUR': 1.2, 'XRP/EUR': 1.5,
        'SOL/EUR': 1.8, 'AVAX/EUR': 1.8, 'LINK/EUR': 1.8,
        'DOGE/EUR': 1.8, 'ADA/EUR': 1.8, 'DOT/EUR': 1.8, 'SUI/EUR': 1.8,
        'NEAR/EUR': 3.0, 'RENDER/EUR': 3.0,
    },
    'TTP_PEAK': 3.0, 'TTP_GIVEBACK': 1.0,
    'PLOCK_PEAK': 5.0, 'PLOCK_DROP': 3.0,
    'STOP_PCT': -3.5,
    'BE_PEAK': 1.0, 'BE_FLOOR': 0.6,
    'TIME_STOP_H': 1.0,
    'STALE_MIN': 30.0, 'STALE_PL_PCT': 1.0,
    'ROTATION_ENTRY_PCT': 2.5,
}))

# 3. Tiered + wider stops + longer time
configs.append(("TIERED-v2 (wider)", {
    'ENTRY_PCT_DEFAULT': 2.0,
    'ENTRY_PCT': {
        'BTC/EUR': 1.2, 'ETH/EUR': 1.2, 'XRP/EUR': 1.5,
        'SOL/EUR': 1.8, 'AVAX/EUR': 1.8, 'LINK/EUR': 1.8,
        'DOGE/EUR': 1.8, 'ADA/EUR': 1.8, 'DOT/EUR': 1.8, 'SUI/EUR': 1.8,
        'NEAR/EUR': 3.0, 'RENDER/EUR': 3.0,
    },
    'TTP_PEAK': 4.0, 'TTP_GIVEBACK': 1.5,
    'PLOCK_PEAK': 7.0, 'PLOCK_DROP': 4.0,
    'STOP_PCT': -5.0,
    'BE_PEAK': 1.5, 'BE_FLOOR': 0.8,
    'TIME_STOP_H': 2.0,
    'STALE_MIN': 45.0, 'STALE_PL_PCT': 1.5,
    'ROTATION_ENTRY_PCT': 3.0,
}))

# 4. Only high-vol coins (NEAR, RENDER, SUI, DOT)
highvol_only = {'NEAR/EUR': 3.0, 'RENDER/EUR': 3.0, 'SUI/EUR': 2.5, 'DOT/EUR': 2.0}
configs.append(("HIGHVOL-ONLY", {
    'ENTRY_PCT_DEFAULT': 99.0,  # effectively disable others
    'ENTRY_PCT': highvol_only,
    'TTP_PEAK': 4.0, 'TTP_GIVEBACK': 1.5,
    'PLOCK_PEAK': 7.0, 'PLOCK_DROP': 4.0,
    'STOP_PCT': -5.0,
    'BE_PEAK': 1.5, 'BE_FLOOR': 0.8,
    'TIME_STOP_H': 2.0,
    'STALE_MIN': 45.0, 'STALE_PL_PCT': 1.5,
    'ROTATION_ENTRY_PCT': 3.0,
}))

# 5. No time-stop, wider everything
configs.append(("NO-TIMESTOP", {
    'ENTRY_PCT_DEFAULT': 2.0,
    'ENTRY_PCT': {
        'BTC/EUR': 1.2, 'ETH/EUR': 1.2, 'XRP/EUR': 1.5,
        'SOL/EUR': 1.8, 'AVAX/EUR': 1.8, 'LINK/EUR': 1.8,
        'DOGE/EUR': 1.8, 'ADA/EUR': 1.8, 'DOT/EUR': 1.8, 'SUI/EUR': 1.8,
        'NEAR/EUR': 3.0, 'RENDER/EUR': 3.0,
    },
    'TTP_PEAK': 5.0, 'TTP_GIVEBACK': 2.0,
    'PLOCK_PEAK': 8.0, 'PLOCK_DROP': 5.0,
    'STOP_PCT': -6.0,
    'BE_PEAK': 2.0, 'BE_FLOOR': 1.0,
    'TIME_STOP_H': 999.0,  # disabled
    'STALE_MIN': 60.0, 'STALE_PL_PCT': 2.0,
    'ROTATION_ENTRY_PCT': 3.5,
}))

# 6. Very tight: scalp the fakeout
configs.append(("SCALP", {
    'ENTRY_PCT_DEFAULT': 2.0,
    'ENTRY_PCT': {
        'NEAR/EUR': 2.5, 'RENDER/EUR': 2.5, 'SUI/EUR': 2.0, 'DOT/EUR': 2.0,
        'SOL/EUR': 2.0, 'AVAX/EUR': 2.0, 'LINK/EUR': 2.0,
    },
    'TTP_PEAK': 2.5, 'TTP_GIVEBACK': 0.5,
    'PLOCK_PEAK': 4.0, 'PLOCK_DROP': 2.5,
    'STOP_PCT': -2.0,
    'BE_PEAK': 0.8, 'BE_FLOOR': 0.4,
    'TIME_STOP_H': 0.5,
    'STALE_MIN': 20.0, 'STALE_PL_PCT': 0.5,
    'ROTATION_ENTRY_PCT': 2.5,
}))

for label, params in configs:
    all_trades, trades_by_sym = run_backtest(all_bars, params)
    summarize(all_trades, trades_by_sym, label)
    # Per-sym breakdown for interesting ones
    if label in ("CURRENT", "TIERED-v2 (wider)", "HIGHVOL-ONLY", "NO-TIMESTOP"):
        for sym in sorted(trades_by_sym.keys()):
            t = trades_by_sym[sym]
            if t:
                w = [x for x in t if x > 0]
                l = [x for x in t if x <= 0]
                wr_s = len(w) / len(t) * 100
                print(f"    {sym}: trades={len(t)} WR={wr_s:.1f}% net={sum(t):+.1f}%")
        print()
