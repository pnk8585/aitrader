"""
Quick Momentum Scalp backtest -- optimized for fast, small profits on crypto.

STRATEGY (per symbol, 15m candles):
  ENTRY (either trigger, plus a volume confirmation):
    1. Breakout : candle close > highest HIGH of the previous 8 bars (last 2 hours)
    2. Vol-pop  : candle close > upper Bollinger Band (20-period SMA + 2 sigma)
    Volume filter: bar volume > 1.5x the average volume of the last 20 bars.
  EXITS (whichever hits first):
    - Take-profit : +2.0%   (quick grab)
    - Stop-loss   : -1.5%
    - Time-stop   : 45 minutes (3 bars of 15m)
  NO trailing, NO breakeven, NO profit-lock. Simple and fast.
  Max 3 open positions globally.
Fees: per-side taker (0.26%) applied on entry and exit. Entry on next bar open + slippage.

Data: 30 days of 15m candles per pair (~2880 bars), public Kraken REST, stdlib only.
Universe: BTC ETH SOL AVAX LINK XRP ADA DOT (no NEAR/RENDER/SUI/DOGE).
"""
import sys
import json
import math
import urllib.request
from datetime import datetime, timezone

# CCXT pair -> Kraken REST altname
PAIRS = {
    "BTC/EUR": "XBTEUR", "ETH/EUR": "ETHEUR", "SOL/EUR": "SOLEUR",
    "AVAX/EUR": "AVAXEUR", "LINK/EUR": "LINKEUR", "XRP/EUR": "XRPEUR",
    "ADA/EUR": "ADAEUR", "DOT/EUR": "DOTEUR",
}

INTERVAL = 15               # minutes per candle
BARS_NEEDED = 2880          # ~30 days of 15m candles
PER_SIDE_FEE = 0.0026       # Kraken taker ~0.26% each side
ENTRY_SLIPPAGE = 0.001      # 0.1% market-order slippage on entry

# --- Strategy parameters ---
BREAKOUT_LOOKBACK = 8       # highest high of last 8 bars (2h)
BB_PERIOD = 20              # Bollinger Band SMA period
BB_STD_MULT = 2.0           # Bollinger Band sigma multiplier
VOL_PERIOD = 20             # volume average lookback
VOL_MULT = 1.5              # require vol > 1.5x avg
TAKE_PROFIT = 2.0           # +2.0% target
STOP_LOSS = -1.5            # -1.5% stop
TIME_STOP_BARS = 3          # 45 min = 3 bars of 15m
MAX_POSITIONS = 3


def fetch(altname, bars_needed=BARS_NEEDED):
    """Fetch up to bars_needed 15m candles, paginating (Kraken maxes at 720/call)."""
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


def compute_bollinger(bars, period=BB_PERIOD, mult=BB_STD_MULT):
    """Upper Bollinger Band per bar (SMA + mult*sigma of closes). None during warmup."""
    n = len(bars)
    upper = [None] * n
    for i in range(period - 1, n):
        window = [bars[j][4] for j in range(i - period + 1, i + 1)]
        mean = sum(window) / period
        var = sum((x - mean) ** 2 for x in window) / period
        upper[i] = mean + mult * math.sqrt(var)
    return upper


def compute_vol_avg(bars, period=VOL_PERIOD):
    """Trailing average volume over the previous `period` bars (excludes current). None during warmup."""
    n = len(bars)
    avg = [None] * n
    for i in range(period, n):
        window = [bars[j][5] for j in range(i - period, i)]
        avg[i] = sum(window) / period
    return avg


def highest_high(bars, i, lookback=BREAKOUT_LOOKBACK):
    """Highest HIGH of the `lookback` bars before bar i. None if not enough history."""
    if i < lookback:
        return None
    return max(bars[j][2] for j in range(i - lookback, i))


def main():
    # Fetch all data first
    all_bars = {}
    for sym, alt in PAIRS.items():
        try:
            bars = fetch(alt)
            if len(bars) < 50:
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

    # Precompute indicators per symbol
    sym_bb = {sym: compute_bollinger(bars) for sym, bars in all_bars.items()}
    sym_volavg = {sym: compute_vol_avg(bars) for sym, bars in all_bars.items()}

    positions = {}                              # sym -> pos dict
    trades_by_sym = {sym: [] for sym in syms}   # sym -> list of net % returns

    for i in range(min_len):
        # --- 1. Manage open positions (exits checked first) ---
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
            # Intrabar: stop-loss and take-profit can both be touched; assume worst (stop) first.
            if lo_pct <= STOP_LOSS:
                exit_pct = STOP_LOSS
            elif hi_pct >= TAKE_PROFIT:
                exit_pct = TAKE_PROFIT
            elif age_bars >= TIME_STOP_BARS:
                exit_pct = cl_pct

            if exit_pct is not None:
                net = exit_pct - PER_SIDE_FEE * 100.0 * 2
                trades_by_sym[sym].append(net)
                del positions[sym]

        # --- 2. Evaluate entries ---
        for sym in syms:
            if sym in positions:
                continue
            if len(positions) >= MAX_POSITIONS:
                break
            b = all_bars[sym][i]
            ts, o, h, l, c, v = b

            # Volume confirmation
            vavg = sym_volavg[sym][i]
            if vavg is None or vavg <= 0 or v <= VOL_MULT * vavg:
                continue

            # Entry trigger: breakout OR Bollinger pop
            hh = highest_high(all_bars[sym], i)
            bb_up = sym_bb[sym][i]
            breakout = hh is not None and c > hh
            vol_pop = bb_up is not None and c > bb_up
            if not (breakout or vol_pop):
                continue

            # Enter on next bar open + slippage
            if i + 1 < len(all_bars[sym]):
                entry_price = all_bars[sym][i + 1][1] * (1 + ENTRY_SLIPPAGE)
            else:
                entry_price = c * (1 + ENTRY_SLIPPAGE)
            positions[sym] = {"entry": entry_price, "i0": i + 1,
                              "trigger": "breakout" if breakout else "bb"}

    # Close any remaining open positions at final bar
    for sym, pos in list(positions.items()):
        last = all_bars[sym][-1]
        cl_pct = (last[4] - pos["entry"]) / pos["entry"] * 100.0
        net = cl_pct - PER_SIDE_FEE * 100.0 * 2
        trades_by_sym[sym].append(net)
        del positions[sym]

    # --- Stats ---
    for sym in syms:
        t = trades_by_sym[sym]
        span = (all_bars[sym][-1][0] - all_bars[sym][0][0]) / 86400000.0
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
    expectancy = total / n
    eq = 1.0
    for x in all_trades:
        eq *= (1 + 0.5 * x / 100.0)        # 50% sizing proxy compounding
    be_wr = (-avg_l) / (avg_w - avg_l) * 100 if (avg_w - avg_l) else 0.0

    print("\n=========== QUICK MOMENTUM SCALP (fee-aware) ===========")
    print(f"Data: {INTERVAL}m candles, {len(syms)} pairs, per-side fee {PER_SIDE_FEE*100:.2f}%")
    print(f"Rules: TP +{TAKE_PROFIT:.1f}% / SL {STOP_LOSS:.1f}% / time-stop "
          f"{TIME_STOP_BARS*INTERVAL}min / max {MAX_POSITIONS} pos")
    print(f"Total round-trip trades : {n}")
    print(f"Win rate                : {wr:.1f}%   ({len(wins)}W / {len(losses)}L)")
    print(f"Avg win                 : +{avg_w:.2f}%")
    print(f"Avg loss                : {avg_l:.2f}%")
    print(f"Expectancy / trade      : {expectancy:+.3f}%   (net of fees)")
    print(f"Sum of net returns      : {total:+.1f}%   (additive, 100% sizing)")
    print(f"Compounded equity (50%) : x{eq:.3f}   ({(eq-1)*100:+.1f}%)")
    print(f"Break-even win rate req. : {be_wr:.1f}%   -> edge is "
          f"{'POSITIVE' if wr > be_wr else 'NEGATIVE'}")
    print("========================================================")

    # Per-symbol breakdown
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
