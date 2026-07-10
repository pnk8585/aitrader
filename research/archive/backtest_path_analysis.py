"""
Analyze what happens AFTER a +2% signal is triggered.
Does price continue up, or immediately reverse?
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

# For each signal bar (close >= +2% from day open), analyze forward path
print("SIGNAL PATH ANALYSIS: What happens after +2% signal?")
print("=" * 70)

for sym, alt in PAIRS.items():
    try:
        bars = fetch(alt, bars_needed=720)
    except Exception as e:
        continue
    if len(bars) < 50:
        continue

    # Build day opens
    dopen = {}
    for b in bars:
        d = day_key(b[0])
        if d not in dopen:
            dopen[d] = b[1]

    signals = []
    for i, b in enumerate(bars):
        ts, o, h, l, c, v = b
        d = day_key(ts)
        do = dopen.get(d)
        if not do:
            continue
        signal_pct = (c - do) / do * 100.0
        if signal_pct >= 2.0:
            # Check if we already signaled today
            if signals and day_key(bars[signals[-1]][0]) == d:
                continue  # only first signal per day
            signals.append(i)

    # Analyze forward 1h, 2h, 4h, end-of-day
    results = []
    for si in signals:
        entry_price = bars[si][4]  # signal bar close
        # Forward returns at various horizons
        max_fwd_4h = 0
        min_fwd_4h = 0
        fwd_1h = 0
        fwd_2h = 0
        fwd_4h = 0
        fwd_eod = 0

        bars_1h = min(si + 4, len(bars))  # 4 bars = 1h
        bars_2h = min(si + 8, len(bars))
        bars_4h = min(si + 16, len(bars))

        # Find end of day
        eod_idx = si
        for j in range(si + 1, len(bars)):
            if day_key(bars[j][0]) == day_key(bars[si][0]):
                eod_idx = j
            else:
                break

        if bars_1h < len(bars):
            fwd_1h = (bars[bars_1h][4] - entry_price) / entry_price * 100
        if bars_2h < len(bars):
            fwd_2h = (bars[bars_2h][4] - entry_price) / entry_price * 100
        if bars_4h < len(bars):
            fwd_4h = (bars[bars_4h][4] - entry_price) / entry_price * 100
        fwd_eod = (bars[eod_idx][4] - entry_price) / entry_price * 100

        # Max/min drawup/drawdown in next 4h
        for j in range(si + 1, min(si + 17, len(bars))):
            hi = (bars[j][2] - entry_price) / entry_price * 100
            lo = (bars[j][3] - entry_price) / entry_price * 100
            max_fwd_4h = max(max_fwd_4h, hi)
            min_fwd_4h = min(min_fwd_4h, lo)

        results.append({
            'fwd_1h': fwd_1h, 'fwd_2h': fwd_2h, 'fwd_4h': fwd_4h,
            'fwd_eod': fwd_eod, 'max_4h': max_fwd_4h, 'min_4h': min_fwd_4h,
            'signal_pct': (bars[si][4] - dopen[day_key(bars[si][0])]) / dopen[day_key(bars[si][0])] * 100
        })

    if not results:
        print(f"{sym:12s}: no signals")
        continue

    avg_sig = sum(r['signal_pct'] for r in results) / len(results)
    avg_1h = sum(r['fwd_1h'] for r in results) / len(results)
    avg_2h = sum(r['fwd_2h'] for r in results) / len(results)
    avg_4h = sum(r['fwd_4h'] for r in results) / len(results)
    avg_eod = sum(r['fwd_eod'] for r in results) / len(results)
    avg_max = sum(r['max_4h'] for r in results) / len(results)
    avg_min = sum(r['min_4h'] for r in results) / len(results)

    # Win rate if held to each horizon
    wr_1h = sum(1 for r in results if r['fwd_1h'] > 0) / len(results) * 100
    wr_2h = sum(1 for r in results if r['fwd_2h'] > 0) / len(results) * 100
    wr_4h = sum(1 for r in results if r['fwd_4h'] > 0) / len(results) * 100
    wr_eod = sum(1 for r in results if r['fwd_eod'] > 0) / len(results) * 100

    print(f"{sym:12s} n={len(results):2d}  sig={avg_sig:+.2f}%  "
          f"1h={avg_1h:+.2f}%(WR{wr_1h:.0f}%)  "
          f"2h={avg_2h:+.2f}%(WR{wr_2h:.0f}%)  "
          f"4h={avg_4h:+.2f}%(WR{wr_4h:.0f}%)  "
          f"EOD={avg_eod:+.2f}%(WR{wr_eod:.0f}%)  "
          f"max4h={avg_max:+.2f}%  min4h={avg_min:+.2f}%")
