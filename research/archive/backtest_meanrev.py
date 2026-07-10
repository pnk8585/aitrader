"""
Backtest: Quick Mean-Reversion Scalp (LONG only) on crypto 15m candles.

Concept: buy a sharp dip below the short SMA, sell the bounce. Keep it simple --
no trailing, no breakeven, no profit-lock. Fixed TP / SL / time-stop.

  ENTRY (LONG only, evaluated at each bar close):
    - close <= SMA(20) * (1 - 2.0%)         (price dipped >= 2% under the 5h SMA)
    - volume > 1.2x average volume of last 20 bars  (real move, not noise)
    - entry on NEXT bar open + 0.1% slippage
  EXITS (whichever hits first, checked intrabar on subsequent bars):
    - Profit target : +1.5%
    - Stop-loss     : -2.0%
    - Time-stop     : 1 hour (4 bars of 15m); close at that bar's close
  FILTERS:
    - Max 3 positions at once (global)
    - 2h cooldown per symbol after an exit (8 bars of 15m)
    - Skip if symbol already entered that UTC day
  UNIVERSE: BTC ETH SOL AVAX LINK XRP ADA DOT (EUR pairs)
  DATA: Kraken public REST OHLC, ~720 bars of 15m (~7.5 days). stdlib only.

Output: total trades, win rate, avg win/loss, expectancy, equity, per-symbol breakdown.
"""
import sys
import json
import urllib.request
from datetime import datetime, timezone

# CCXT pair -> Kraken REST altname (8 coins, no shitcoins)
PAIRS = {
    "BTC/EUR": "XBTEUR", "ETH/EUR": "ETHEUR", "SOL/EUR": "SOLEUR",
    "AVAX/EUR": "AVAXEUR", "LINK/EUR": "LINKEUR", "XRP/EUR": "XRPEUR",
    "ADA/EUR": "ADAEUR", "DOT/EUR": "DOTEUR",
}

INTERVAL = 15               # minutes per candle
PER_SIDE_FEE = 0.0026       # Kraken taker ~0.26% each side
BARS_NEEDED = 720           # ~7.5 days of 15m candles

# --- Strategy params ---
SMA_PERIOD = 20             # 20 bars = 5h
VOL_PERIOD = 20             # avg volume lookback
DIP_PCT = 2.0               # entry: close >= 2% below SMA
VOL_MULT = 1.2              # entry: volume > 1.2x avg
TP_PCT = 1.5                # profit target +1.5%
SL_PCT = -2.0               # stop-loss -2.0%
TIME_STOP_BARS = 4          # 1 hour = 4 bars of 15m
ENTRY_SLIPPAGE = 0.001      # 0.1% slippage on entry
MAX_POSITIONS = 3
COOLDOWN_BARS = 8           # 2h cooldown per symbol after an exit


def fetch(altname, bars_needed=BARS_NEEDED):
    """Fetch up to bars_needed 15m candles (Kraken maxes at 720/call, paginate if more)."""
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


def compute_sma(bars, period=SMA_PERIOD):
    """Simple moving average of close, per-bar. None during warmup (< period bars)."""
    n = len(bars)
    sma = [None] * n
    run = 0.0
    for i in range(n):
        run += bars[i][4]
        if i >= period:
            run -= bars[i - period][4]
        if i >= period - 1:
            sma[i] = run / period
    return sma


def compute_vol_avg(bars, period=VOL_PERIOD):
    """Trailing average volume over the PRIOR `period` bars (excludes current). None in warmup."""
    n = len(bars)
    va = [None] * n
    run = 0.0
    for i in range(n):
        if i >= period:
            run -= bars[i - period][5]
        if i >= 1:
            run += bars[i - 1][5]
        if i >= period:                 # have `period` prior bars
            va[i] = run / period
    return va


def main():
    # Fetch all data
    all_bars = {}
    for sym, alt in PAIRS.items():
        try:
            bars = fetch(alt)
            if len(bars) < SMA_PERIOD + 5:
                print(f"  {sym:9s} skipped ({len(bars)} bars)", file=sys.stderr)
                continue
            all_bars[sym] = bars
        except Exception as e:
            print(f"  {sym:9s} ERROR {e}", file=sys.stderr)

    if not all_bars:
        print("No data fetched.")
        return

    syms = list(all_bars.keys())
    min_len = min(len(b) for b in all_bars.values())

    sym_sma = {s: compute_sma(all_bars[s]) for s in syms}
    sym_vol = {s: compute_vol_avg(all_bars[s]) for s in syms}

    positions = {}                                   # sym -> pos dict
    trades_by_sym = {s: [] for s in syms}
    entered_today = {s: set() for s in syms}         # UTC day_keys already entered
    cooldown_until = {s: -1 for s in syms}           # bar index until re-entry blocked

    for i in range(min_len):
        # --- Manage open positions (exits) ---
        for sym in list(positions.keys()):
            pos = positions[sym]
            b = all_bars[sym][i]
            ts, o, h, l, c, v = b
            entry = pos["entry"]
            hi_pct = (h - entry) / entry * 100.0
            lo_pct = (l - entry) / entry * 100.0
            cl_pct = (c - entry) / entry * 100.0
            age_bars = i - pos["i0"]

            exit_pct = None
            if lo_pct <= SL_PCT:                     # stop-loss intrabar
                exit_pct = SL_PCT
            elif hi_pct >= TP_PCT:                   # profit target intrabar
                exit_pct = TP_PCT
            elif age_bars >= TIME_STOP_BARS:         # time-stop -> close at this bar's close
                exit_pct = cl_pct

            if exit_pct is not None:
                net = exit_pct - PER_SIDE_FEE * 100.0 * 2
                trades_by_sym[sym].append(net)
                del positions[sym]
                cooldown_until[sym] = i + COOLDOWN_BARS

        # --- Evaluate entries ---
        # Collect candidate entry signals at this bar
        candidates = []
        for sym in syms:
            if sym in positions:
                continue
            if i + 1 >= len(all_bars[sym]):          # need a next bar to enter on
                continue
            b = all_bars[sym][i]
            ts, o, h, l, c, v = b
            d = day_key(ts)
            if d in entered_today[sym]:
                continue
            if i < cooldown_until[sym]:
                continue
            sma = sym_sma[sym][i]
            vavg = sym_vol[sym][i]
            if sma is None or vavg is None or sma <= 0 or vavg <= 0:
                continue
            dip_pct = (c - sma) / sma * 100.0        # negative when below SMA
            if dip_pct > -DIP_PCT:                   # not dipped enough
                continue
            if v <= vavg * VOL_MULT:                 # volume not confirming
                continue
            # deeper dip = stronger signal; rank by it
            candidates.append((sym, dip_pct, d))

        candidates.sort(key=lambda x: x[1])          # most negative (deepest dip) first
        for sym, dip_pct, d in candidates:
            if len(positions) >= MAX_POSITIONS:
                break
            next_open = all_bars[sym][i + 1][1]
            entry_price = next_open * (1 + ENTRY_SLIPPAGE)
            positions[sym] = {"entry": entry_price, "i0": i}
            entered_today[sym].add(d)

    # Close any remaining open positions at final bar
    for sym, pos in list(positions.items()):
        last = all_bars[sym][-1]
        cl_pct = (last[4] - pos["entry"]) / pos["entry"] * 100.0
        net = cl_pct - PER_SIDE_FEE * 100.0 * 2
        trades_by_sym[sym].append(net)
        del positions[sym]

    # Per-symbol fetch log
    for sym in syms:
        span = (all_bars[sym][-1][0] - all_bars[sym][0][0]) / 86400000.0
        t = trades_by_sym[sym]
        print(f"  {sym:9s} bars={len(all_bars[sym]):4d} ({span:.1f}d) trades={len(t):3d} "
              f"net={sum(t):+.1f}%", file=sys.stderr)

    all_trades = []
    for t in trades_by_sym.values():
        all_trades += t
    if not all_trades:
        print("No trades triggered in the window.")
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
        eq *= (1 + 0.5 * x / 100.0)                  # 50% sizing proxy
    be_wr = (-avg_l) / (avg_w - avg_l) * 100 if (avg_w - avg_l) else 0.0

    print("\n============ BACKTEST: Quick Mean-Reversion Scalp (fee-aware) ============")
    print(f"Data: {INTERVAL}m candles, {len(syms)} pairs, per-side fee {PER_SIDE_FEE*100:.2f}%")
    print(f"Rules: dip<=-{DIP_PCT}% vs SMA{SMA_PERIOD}, vol>{VOL_MULT}x avg{VOL_PERIOD} | "
          f"TP +{TP_PCT}% / SL {SL_PCT}% / time {TIME_STOP_BARS} bars")
    print(f"Total round-trip trades : {n}")
    print(f"Win rate                : {wr:.1f}%   ({len(wins)}W / {len(losses)}L)")
    print(f"Avg win                 : +{avg_w:.2f}%")
    print(f"Avg loss                : {avg_l:.2f}%")
    print(f"Expectancy / trade      : {total/n:+.3f}%   (net of fees)")
    print(f"Sum of net returns      : {total:+.1f}%   (additive, 100% sizing)")
    print(f"Compounded equity (50%) : x{eq:.3f}   ({(eq-1)*100:+.1f}%)")
    print(f"Break-even win rate req. : {be_wr:.1f}%   -> edge is "
          f"{'POSITIVE' if wr > be_wr else 'NEGATIVE'}")
    print("=========================================================================")

    print("\n--- Per-symbol breakdown ---")
    for sym in sorted(trades_by_sym.keys()):
        t = trades_by_sym[sym]
        if not t:
            print(f"{sym:9s}: no trades")
            continue
        w = [x for x in t if x > 0]
        l = [x for x in t if x <= 0]
        wr_s = len(w) / len(t) * 100
        aw = sum(w) / len(w) if w else 0.0
        al = sum(l) / len(l) if l else 0.0
        exp = sum(t) / len(t)
        print(f"{sym:9s}: trades={len(t):3d}  WR={wr_s:.1f}%  avgW={aw:+.2f}%  "
              f"avgL={al:+.2f}%  exp={exp:+.3f}%  net={sum(t):+.1f}%")


if __name__ == "__main__":
    main()
