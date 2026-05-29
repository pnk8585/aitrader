"""
Backtest for the crypto-momentum strategy in execute_kraken_cycle.py / execute_cycle.py.

Simulates the CURRENT live rules on real historical OHLC (public Kraken REST, no auth,
no third-party deps -- stdlib only):

  ENTRY : intraday change vs UTC-day open >= 2.0%  (one position at a time, per symbol)
  EXITS : Trailing-Take-Profit (peak>=3.0, give back 1.0)   [live: TTP_PEAK/GIVEBACK]
          Profit-lock        (peak>=5.0, drop <3.0)         [live: PLOCK_PEAK/FLOOR]
          Stop-loss          (<= -3.5%)
          Breakeven fee-aware (peak>=1.0, drop <= fee floor)
          Time-stop          (held > 1.0h)
Fees: per-side taker applied on entry and exit.

Measures PER-SYMBOL expectancy of the edge (cleanest read on net profitability).
Data: latest ~720 bars of 15m candles per pair (~7.5 days). First-look estimate.
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

# Matches live wired values in execute_*_cycle.py (ride-the-wave tuning)
ENTRY_PCT = 2.0
TTP_PEAK, TTP_GIVEBACK = 3.0, 1.0
PLOCK_PEAK, PLOCK_DROP = 5.0, 3.0
STOP_PCT = -3.5
BE_PEAK, BE_FLOOR = 1.0, 0.6
TIME_STOP_H = 1.0


def fetch(altname):
    url = f"https://api.kraken.com/0/public/OHLC?pair={altname}&interval={INTERVAL}"
    req = urllib.request.Request(url, headers={"User-Agent": "backtest/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())
    if data.get("error"):
        raise RuntimeError(", ".join(data["error"]))
    result = data["result"]
    key = next(k for k in result if k != "last")
    rows = result[key]
    # row: [time, open, high, low, close, vwap, volume, count]
    return [[int(x[0]) * 1000, float(x[1]), float(x[2]), float(x[3]),
             float(x[4]), float(x[6])] for x in rows]


def day_key(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def simulate(bars):
    """Return list of realized round-trip net returns (%, after fees) for one symbol."""
    day_open = {}
    for b in bars:
        d = day_key(b[0])
        if d not in day_open:
            day_open[d] = b[1]

    trades = []
    pos = None
    bars_per_h = 60.0 / INTERVAL
    for i, b in enumerate(bars):
        ts, o, h, l, c, v = b
        dopen = day_open.get(day_key(ts))
        if not dopen or dopen <= 0:
            continue

        if pos is None:
            if (c - dopen) / dopen * 100.0 >= ENTRY_PCT:
                pos = {"entry": c, "peak": 0.0, "i0": i}
            continue

        entry = pos["entry"]
        hi_pct = (h - entry) / entry * 100.0
        lo_pct = (l - entry) / entry * 100.0
        cl_pct = (c - entry) / entry * 100.0
        pos["peak"] = max(pos["peak"], hi_pct)
        peak = pos["peak"]
        age_h = (i - pos["i0"]) / bars_per_h

        exit_pct = None
        if lo_pct <= STOP_PCT:                                   # stop-loss intrabar
            exit_pct = STOP_PCT
        elif peak >= TTP_PEAK and cl_pct <= (peak - TTP_GIVEBACK):
            exit_pct = cl_pct
        elif peak >= PLOCK_PEAK and cl_pct < PLOCK_DROP:
            exit_pct = cl_pct
        elif peak >= BE_PEAK and cl_pct <= BE_FLOOR:
            exit_pct = cl_pct
        elif age_h > TIME_STOP_H:
            exit_pct = cl_pct

        if exit_pct is not None:
            trades.append(exit_pct - PER_SIDE_FEE * 100.0 * 2)
            pos = None
    return trades


def main():
    all_trades, per_sym = [], {}
    for sym, alt in PAIRS.items():
        try:
            bars = fetch(alt)
            if len(bars) < 50:
                print(f"  {sym:11s} skipped ({len(bars)} bars)", file=sys.stderr)
                continue
            t = simulate(bars)
            per_sym[sym] = t
            span = (bars[-1][0] - bars[0][0]) / 86400000.0
            print(f"  {sym:11s} bars={len(bars):4d} ({span:.1f}d) trades={len(t):3d} "
                  f"net={sum(t):+.1f}%", file=sys.stderr)
        except Exception as e:
            print(f"  {sym:11s} ERROR {e}", file=sys.stderr)

    if not all_trades and not per_sym:
        print("No data fetched.")
        return
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


if __name__ == "__main__":
    main()
