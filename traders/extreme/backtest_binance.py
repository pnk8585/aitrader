"""
Backtest: Mean-Reversion Quick Scalp on Binance 15m data (free API, no auth).
Data: 1000 bars (~10.4 days) per pair, paginated for 30 days total.
Strategy: Buy dips (-3% below 20-bar SMA), sell bounce (+1% target).
"""
import sys
import json
import urllib.request
from datetime import datetime, timezone

PAIRS = {
    "BTC/EUR": "BTCEUR", "ETH/EUR": "ETHEUR", "SOL/EUR": "SOLEUR",
    "AVAX/EUR": "AVAXEUR", "LINK/EUR": "LINKEUR", "XRP/EUR": "XRPEUR",
    "ADA/EUR": "ADAEUR", "DOT/EUR": "DOTEUR",
}

INTERVAL = "15m"
BARS_PER_PAGE = 1000
BARS_NEEDED = 2880  # ~30 days
PER_SIDE_FEE = 0.001  # Binance taker ~0.1%
ENTRY_SLIPPAGE = 0.001

ENTRY_DIP_PCT = -3.0
TP_PCT = 1.0
SL_PCT = -2.0
TIME_STOP_BARS = 8  # 2 hours
MAX_POS = 3
COOLDOWN_BARS = 8  # 2 hours
SMA_BARS = 20
VOL_MULT = 1.2


def fetch(symbol):
    """Fetch up to BARS_NEEDED 15m candles from Binance, paginating backwards."""
    all_rows = []
    end_time = None
    while len(all_rows) < BARS_NEEDED:
        url = (f"https://api.binance.com/api/v3/klines?symbol={symbol}"
               f"&interval={INTERVAL}&limit={BARS_PER_PAGE}")
        if end_time:
            url += f"&endTime={end_time}"
        req = urllib.request.Request(url, headers={"User-Agent": "backtest/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            rows = json.loads(r.read().decode())
        if not rows:
            break
        # rows: [openTime, open, high, low, close, volume, closeTime, ...]
        parsed = [[int(x[0]), float(x[1]), float(x[2]), float(x[3]),
                   float(x[4]), float(x[5])] for x in rows]
        all_rows = parsed + all_rows
        end_time = parsed[0][0] - 1
        if len(rows) < BARS_PER_PAGE:
            break
    return all_rows[-BARS_NEEDED:] if len(all_rows) > BARS_NEEDED else all_rows


def compute_sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def simulate(bars):
    """Return list of net returns (%) for this symbol."""
    trades = []
    pos = None
    cooldown_until = -1
    entered_today = set()

    for i in range(SMA_BARS, len(bars) - 1):
        ts, o, h, l, c, v = bars[i]
        d = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

        # Compute SMA and avg volume
        closes = [b[4] for b in bars[:i]]
        volumes = [b[5] for b in bars[:i]]
        sma = compute_sma(closes, SMA_BARS)
        avg_vol = compute_sma(volumes, SMA_BARS)
        if sma is None or avg_vol is None or sma <= 0:
            continue

        # Manage existing position
        if pos is not None:
            entry = pos["entry"]
            hi_pct = (h - entry) / entry * 100.0
            lo_pct = (l - entry) / entry * 100.0
            cl_pct = (c - entry) / entry * 100.0
            age = i - pos["i0"]

            exit_pct = None
            if lo_pct <= SL_PCT:
                exit_pct = SL_PCT
            elif hi_pct >= TP_PCT:
                exit_pct = TP_PCT
            elif age >= TIME_STOP_BARS:
                exit_pct = cl_pct

            if exit_pct is not None:
                net = exit_pct - PER_SIDE_FEE * 100.0 * 2
                trades.append(net)
                pos = None
                cooldown_until = i + COOLDOWN_BARS
                entered_today.discard(d)
                continue

        # Check entry
        if pos is None and i > cooldown_until and d not in entered_today:
            dip_pct = (c - sma) / sma * 100.0
            if dip_pct <= ENTRY_DIP_PCT and v > avg_vol * VOL_MULT:
                next_open = bars[i + 1][1]
                entry_price = next_open * (1 + ENTRY_SLIPPAGE)
                pos = {"entry": entry_price, "i0": i}
                entered_today.add(d)

    # Close any open position at last bar
    if pos is not None:
        last = bars[-1]
        cl_pct = (last[4] - pos["entry"]) / pos["entry"] * 100.0
        net = cl_pct - PER_SIDE_FEE * 100.0 * 2
        trades.append(net)

    return trades


def main():
    all_bars = {}
    for sym, bin_sym in PAIRS.items():
        try:
            bars = fetch(bin_sym)
            if len(bars) < 100:
                print(f"  {sym:11s} skipped ({len(bars)} bars)", file=sys.stderr)
                continue
            all_bars[sym] = bars
            span = (bars[-1][0] - bars[0][0]) / 86400000.0
            print(f"  {sym:11s} bars={len(bars):4d} ({span:.1f}d)", file=sys.stderr)
        except Exception as e:
            print(f"  {sym:11s} ERROR {e}", file=sys.stderr)

    if not all_bars:
        print("No data fetched.")
        return

    # Simulate each symbol independently (no global position limit for simplicity)
    per_sym = {}
    for sym, bars in all_bars.items():
        t = simulate(bars)
        per_sym[sym] = t
        print(f"  {sym:11s} trades={len(t):3d} net={sum(t):+.1f}%", file=sys.stderr)

    all_trades = []
    for t in per_sym.values():
        all_trades += t
    if not all_trades:
        print("No trades triggered.")
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

    print("\n================ MEAN-REVERSION SCALP (Binance 15m) ================")
    print(f"Data: {INTERVAL} candles, ~{len(per_sym)} pairs, per-side fee {PER_SIDE_FEE*100:.2f}%")
    print(f"Entry: dip <= {ENTRY_DIP_PCT}% below {SMA_BARS}-bar SMA, vol > {VOL_MULT}×")
    print(f"Exit: TP +{TP_PCT}%, SL {SL_PCT}%, time-stop {TIME_STOP_BARS} bars")
    print(f"Total trades: {n}  |  WR: {wr:.1f}% ({len(wins)}W/{len(losses)}L)")
    print(f"Avg win: +{avg_w:.2f}%  |  Avg loss: {avg_l:.2f}%")
    print(f"Expectancy: {total/n:+.3f}%  |  Equity: x{eq:.3f} ({(eq-1)*100:+.1f}%)")
    print(f"Break-even WR: {be_wr:.1f}%  |  Edge: {'POSITIVE' if wr > be_wr else 'NEGATIVE'}")
    print("===================================================================")

    print("\n--- Per-symbol ---")
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
        print(f"{sym:11s}: trades={len(t):3d} WR={wr_s:.1f}% avgW={aw:+.2f}% avgL={al:+.2f}% net={sum(t):+.1f}%")


if __name__ == "__main__":
    main()
