"""
Backtest for the crypto-momentum strategy in execute_kraken_cycle.py / execute_cycle.py.

Simulates the CURRENT live rules on real historical OHLC (public Kraken REST, no auth,
no third-party deps -- stdlib only):

  ENTRY : intraday change vs UTC-day open >= 2.0%  (one position at a time, per symbol)
  EXITS : Stop-loss          (<= -1.5x ATR(14) from entry, Wilder ATR)
          Chandelier trail   (close <= peak - 2.5x ATR%, arms immediately, current-bar ATR)
          Trailing-Take-Profit (close <= peak - 1.5, arms immediately at any peak)
          Profit-lock        (peak>=8.0, drop <5.0)
          Time-stop          (held > 4.0h)
          Stale rotation     (held >30 min, P/L < +1.0%, new signal >= +2.5%)
  Cooldown: 2h (8 bars of 15m) per symbol after any exit before re-entry.
  Max positions: 5 globally
Fees: per-side taker applied on entry and exit.

Measures PER-SYMBOL expectancy of the edge (cleanest read on net profitability).
Data: latest ~2880 bars of 15m candles per pair (~30 days), paginated since Kraken
returns max 720 per call.
"""
import sys
import json
import urllib.request
from datetime import datetime, timezone

# CCXT pair -> Kraken REST altname
PAIRS = {
    "BTC/EUR": "XBTEUR", "ETH/EUR": "ETHEUR", "SOL/EUR": "SOLEUR",
    "AVAX/EUR": "AVAXEUR", "LINK/EUR": "LINKEUR", "XRP/EUR": "XRPEUR",
    "DOGE/EUR": "XDGEUR", "SUI/EUR": "SUIEUR", "NEAR/EUR": "NEAREUR",
    "RENDER/EUR": "RENDEREUR", "ADA/EUR": "ADAEUR", "DOT/EUR": "DOTEUR",
}

INTERVAL = 15               # minutes per candle
PER_SIDE_FEE = 0.0026       # Kraken taker ~0.26% each side
MAX_POSITIONS = 5

# Matches live wired values in execute_*_cycle.py (ride-the-wave tuning)
ENTRY_PCT = 2.0
TTP_GIVEBACK = 1.5                 # trailing-take-profit give-back, arms immediately
PLOCK_PEAK, PLOCK_DROP = 8.0, 5.0  # profit-lock
ATR_PERIOD = 14
STOP_ATR_MULT = 1.5                # stop-loss distance = 1.5x ATR (Wilder, ATR at entry)
CHANDELIER_ATR_MULT = 2.5          # chandelier trail = 2.5x ATR% from peak (current bar)
STOP_PCT_FALLBACK = -3.5           # used when ATR not yet available (warmup)
TIME_STOP_H = 4.0
STALE_MIN = 30.0
STALE_PL_PCT = 1.0
ROTATION_ENTRY_PCT = 2.5
ENTRY_SLIPPAGE = 0.001      # 0.1% market-order slippage on entry
COOLDOWN_BARS = 8           # 2h cooldown per symbol after an exit (8 x 15m)


def fetch(altname, bars_needed=2880):
    """Fetch up to bars_needed 15m candles, paginating since Kraken maxes at 720/call."""
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
        # Avoid infinite loop if last page repeats
        if since and parsed and parsed[0][0] == since * 1000:
            parsed = parsed[1:]
        if not parsed:
            break
        all_rows.extend(parsed)
        since = int(rows[-1][0])
        if len(rows) < 720:
            break
    return all_rows[-bars_needed:] if len(all_rows) > bars_needed else all_rows


def compute_atr(bars, period=ATR_PERIOD):
    """Wilder ATR(period) aligned per-bar. stdlib only, returns list with None during warmup.

    TR_i  = max(high-low, |high-prev_close|, |low-prev_close|)
    ATR_p = simple mean of the first `period` TRs (seeded at bar index `period`)
    ATR_i = (ATR_{i-1} * (period-1) + TR_i) / period   (Wilder smoothing)
    """
    n = len(bars)
    atr = [None] * n
    if n < period + 1:
        return atr
    # trs[k] is the True Range for bar (k+1)
    trs = []
    for i in range(1, n):
        h, l, pc = bars[i][2], bars[i][3], bars[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    # Seed: first ATR at bar index `period` = mean of TRs for bars 1..period
    prev = sum(trs[:period]) / period
    atr[period] = prev
    for i in range(period + 1, n):
        tr = trs[i - 1]
        prev = (prev * (period - 1) + tr) / period
        atr[i] = prev
    return atr


def atr_pct_at(atr, bars, i):
    """ATR as a percentage of the bar-i close, or None during warmup / bad data."""
    if i >= len(atr):
        return None
    v = atr[i]
    if v is None:
        return None
    c = bars[i][4]
    if c <= 0:
        return None
    return v / c * 100.0


def day_key(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def utc_day_open_ms(ms):
    """Return timestamp (ms) for 00:00 UTC of the day containing ms."""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000)


def simulate(bars, symbol, global_positions, global_trades):
    """
    Simulate one symbol, mutating global_positions and global_trades dicts.
    Returns list of realized round-trip net returns (%, after fees) for this symbol.
    """
    # First bar's open per calendar day is the best proxy for the UTC-day open.
    day_open = {}
    for b in bars:
        d = day_key(b[0])
        if d not in day_open:
            day_open[d] = b[1]

    atr = compute_atr(bars)

    trades = []
    pos = None
    entered_today = set()   # day_key strings where we already entered
    cooldown_until = -1     # bar index until which re-entry is blocked
    bars_per_h = 60.0 / INTERVAL

    for i, b in enumerate(bars):
        ts, o, h, l, c, v = b
        d = day_key(ts)
        dopen = day_open.get(d)
        if not dopen or dopen <= 0:
            continue

        # --- Manage existing position ---
        if pos is not None:
            entry = pos["entry"]
            hi_pct = (h - entry) / entry * 100.0
            lo_pct = (l - entry) / entry * 100.0
            cl_pct = (c - entry) / entry * 100.0
            pos["peak"] = max(pos["peak"], hi_pct)
            peak = pos["peak"]
            age_h = (i - pos["i0"]) / bars_per_h
            pl = cl_pct
            atr_pct_cur = atr_pct_at(atr, bars, i)

            exit_pct = None
            if lo_pct <= pos["stop_pct"]:                            # ATR stop-loss intrabar
                exit_pct = pos["stop_pct"]
            elif atr_pct_cur is not None and cl_pct <= (peak - CHANDELIER_ATR_MULT * atr_pct_cur):
                exit_pct = cl_pct                                    # chandelier trail
            elif cl_pct <= (peak - TTP_GIVEBACK):                    # TTP, arms immediately
                exit_pct = cl_pct
            elif peak >= PLOCK_PEAK and cl_pct < PLOCK_DROP:         # profit-lock
                exit_pct = cl_pct
            elif age_h > TIME_STOP_H:                                # time-stop
                exit_pct = cl_pct

            if exit_pct is not None:
                net = exit_pct - PER_SIDE_FEE * 100.0 * 2
                trades.append(net)
                global_trades[symbol].append(net)
                del global_positions[symbol]
                pos = None
                entered_today.discard(d)
                cooldown_until = i + COOLDOWN_BARS
                continue

            # Stale rotation flag (checked below when evaluating new signals)
            pos["stale"] = (age_h > (STALE_MIN / 60.0)) and (pl < STALE_PL_PCT)
            pos["pl"] = pl

        # --- Evaluate entry ---
        signal_pct = (c - dopen) / dopen * 100.0

        if pos is None:
            if signal_pct >= ENTRY_PCT and d not in entered_today and i >= cooldown_until:
                # Check global position limit
                if len(global_positions) >= MAX_POSITIONS:
                    continue
                # Entry on next bar's open + slippage
                if i + 1 < len(bars):
                    next_open = bars[i + 1][1]
                    entry_price = next_open * (1 + ENTRY_SLIPPAGE)
                else:
                    entry_price = c * (1 + ENTRY_SLIPPAGE)
                # ATR stop fixed at entry-bar ATR%
                ap = atr_pct_at(atr, bars, i)
                stop_pct = -STOP_ATR_MULT * ap if ap is not None else STOP_PCT_FALLBACK
                pos = {"entry": entry_price, "peak": 0.0, "i0": i,
                       "stale": False, "pl": 0.0, "stop_pct": stop_pct}
                global_positions[symbol] = pos
                entered_today.add(d)

    # Close any open position at last bar
    if pos is not None:
        last = bars[-1]
        cl_pct = (last[4] - pos["entry"]) / pos["entry"] * 100.0
        net = cl_pct - PER_SIDE_FEE * 100.0 * 2
        trades.append(net)
        global_trades[symbol].append(net)
        del global_positions[symbol]

    return trades


def main():
    global_positions = {}   # sym -> pos dict
    global_trades = {sym: [] for sym in PAIRS}

    # Fetch all data first
    all_bars = {}
    for sym, alt in PAIRS.items():
        try:
            bars = fetch(alt)
            if len(bars) < 50:
                print(f"  {sym:11s} skipped ({len(bars)} bars)", file=sys.stderr)
                continue
            all_bars[sym] = bars
        except Exception as e:
            print(f"  {sym:11s} ERROR {e}", file=sys.stderr)

    if not all_bars:
        print("No data fetched.")
        return

    # Align bars by index using the shortest series as master (Kraken data is aligned).
    min_len = min(len(b) for b in all_bars.values())
    syms = list(all_bars.keys())

    # Precompute day_open per symbol per day
    sym_day_open = {}
    for sym, bars in all_bars.items():
        dopen = {}
        for b in bars:
            d = day_key(b[0])
            if d not in dopen:
                dopen[d] = b[1]
        sym_day_open[sym] = dopen

    # Precompute Wilder ATR per symbol
    sym_atr = {sym: compute_atr(bars) for sym, bars in all_bars.items()}

    # Track entered-today + cooldown per symbol
    entered_today = {sym: set() for sym in syms}
    cooldown_until = {sym: -1 for sym in syms}   # bar index until which re-entry blocked
    positions = {}   # sym -> pos dict
    trades_by_sym = {sym: [] for sym in syms}
    bars_per_h = 60.0 / INTERVAL

    for i in range(min_len):
        # First, manage existing positions (exits)
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
            atr_pct_cur = atr_pct_at(sym_atr[sym], all_bars[sym], i)

            exit_pct = None
            if lo_pct <= pos["stop_pct"]:                            # ATR stop-loss intrabar
                exit_pct = pos["stop_pct"]
            elif atr_pct_cur is not None and cl_pct <= (peak - CHANDELIER_ATR_MULT * atr_pct_cur):
                exit_pct = cl_pct                                    # chandelier trail
            elif cl_pct <= (peak - TTP_GIVEBACK):                    # TTP, arms immediately
                exit_pct = cl_pct
            elif peak >= PLOCK_PEAK and cl_pct < PLOCK_DROP:         # profit-lock
                exit_pct = cl_pct
            elif age_h > TIME_STOP_H:                                # time-stop
                exit_pct = cl_pct

            if exit_pct is not None:
                net = exit_pct - PER_SIDE_FEE * 100.0 * 2
                trades_by_sym[sym].append(net)
                del positions[sym]
                d = day_key(ts)
                entered_today[sym].discard(d)
                cooldown_until[sym] = i + COOLDOWN_BARS
                continue

            pos["stale"] = (age_h > (STALE_MIN / 60.0)) and (pl < STALE_PL_PCT)
            pos["pl"] = pl

        # Compute signals for all symbols at this bar
        signals = []
        for sym in syms:
            b = all_bars[sym][i]
            ts, o, h, l, c, v = b
            d = day_key(ts)
            dopen = sym_day_open[sym].get(d)
            if not dopen or dopen <= 0:
                continue
            signal_pct = (c - dopen) / dopen * 100.0
            signals.append((sym, signal_pct, d, i))

        # Stale rotation: close stale positions if a better signal exists
        stale_syms = [s for s, p in positions.items() if p.get("stale")]
        if stale_syms:
            # Find best signal among symbols not in positions, off cooldown, above rotation thr
            available = [s for s in signals if s[0] not in positions
                         and s[1] >= ROTATION_ENTRY_PCT and i >= cooldown_until[s[0]]]
            available.sort(key=lambda x: x[1], reverse=True)
            for stale_sym in stale_syms:
                if not available:
                    break
                best = available[0]
                # Close stale position
                pos = positions[stale_sym]
                b = all_bars[stale_sym][i]
                cl_pct = (b[4] - pos["entry"]) / pos["entry"] * 100.0
                net = cl_pct - PER_SIDE_FEE * 100.0 * 2
                trades_by_sym[stale_sym].append(net)
                del positions[stale_sym]
                d = day_key(b[0])
                entered_today[stale_sym].discard(d)
                cooldown_until[stale_sym] = i + COOLDOWN_BARS
                # Open new position on next bar
                if best[3] + 1 < len(all_bars[best[0]]):
                    next_open = all_bars[best[0]][best[3] + 1][1]
                    entry_price = next_open * (1 + ENTRY_SLIPPAGE)
                else:
                    entry_price = all_bars[best[0]][best[3]][4] * (1 + ENTRY_SLIPPAGE)
                ap = atr_pct_at(sym_atr[best[0]], all_bars[best[0]], i)
                stop_pct = -STOP_ATR_MULT * ap if ap is not None else STOP_PCT_FALLBACK
                positions[best[0]] = {
                    "entry": entry_price, "peak": 0.0, "i0": i,
                    "stale": False, "pl": 0.0, "stop_pct": stop_pct
                }
                entered_today[best[0]].add(best[2])
                available = [s for s in available if s[0] != best[0]]

        # New entries for symbols without position
        for sym, signal_pct, d, idx in signals:
            if sym in positions:
                continue
            if d in entered_today[sym]:
                continue
            if signal_pct < ENTRY_PCT:
                continue
            if i < cooldown_until[sym]:                              # 2h cooldown
                continue
            if len(positions) >= MAX_POSITIONS:
                continue
            # Entry on next bar open + slippage
            if idx + 1 < len(all_bars[sym]):
                next_open = all_bars[sym][idx + 1][1]
                entry_price = next_open * (1 + ENTRY_SLIPPAGE)
            else:
                entry_price = all_bars[sym][idx][4] * (1 + ENTRY_SLIPPAGE)
            ap = atr_pct_at(sym_atr[sym], all_bars[sym], idx)
            stop_pct = -STOP_ATR_MULT * ap if ap is not None else STOP_PCT_FALLBACK
            positions[sym] = {
                "entry": entry_price, "peak": 0.0, "i0": i,
                "stale": False, "pl": 0.0, "stop_pct": stop_pct
            }
            entered_today[sym].add(d)

    # Close any remaining open positions at final bar
    for sym, pos in list(positions.items()):
        last = all_bars[sym][-1]
        cl_pct = (last[4] - pos["entry"]) / pos["entry"] * 100.0
        net = cl_pct - PER_SIDE_FEE * 100.0 * 2
        trades_by_sym[sym].append(net)
        del positions[sym]

    # Per-symbol stats
    per_sym = trades_by_sym
    for sym, t in per_sym.items():
        span = (all_bars[sym][-1][0] - all_bars[sym][0][0]) / 86400000.0
        print(f"  {sym:11s} bars={len(all_bars[sym]):4d} ({span:.1f}d) trades={len(t):3d} "
              f"net={sum(t):+.1f}%", file=sys.stderr)

    all_trades = []
    for t in per_sym.values():
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
        eq *= (1 + 0.5 * x / 100.0)        # 50% sizing proxy
    be_wr = (-avg_l) / (avg_w - avg_l) * 100 if (avg_w - avg_l) else 0.0

    print("\n================ BACKTEST (current live rules, fee-aware) ================")
    print(f"Data: {INTERVAL}m candles, ~{len(per_sym)} pairs, per-side fee {PER_SIDE_FEE*100:.2f}%")
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

    # Per-symbol summary
    print("\n--- Per-symbol breakdown ---")
    for sym in sorted(per_sym.keys()):
        t = per_sym[sym]
        if not t:
            print(f"{sym:11s}: no trades")
            continue
        w = [x for x in t if x > 0]
        l = [x for x in t if x <= 0]
        wr_s = len(w) / len(t) * 100
        aw = sum(w) / len(w) if w else 0.0
        al = sum(l) / len(l) if l else 0.0
        exp = sum(t) / len(t)
        print(f"{sym:11s}: trades={len(t):3d}  WR={wr_s:.1f}%  avgW={aw:+.2f}%  "
              f"avgL={al:+.2f}%  exp={exp:+.3f}%  net={sum(t):+.1f}%")


if __name__ == "__main__":
    main()
