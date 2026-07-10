"""Pullback entry signal scanning — mirrors live kraken_pullback filters."""

from traders.strategies.pullback import config as C


def scan_pullback_candidates(
    db_conn,
    crypto_pairs,
    tickers: dict,
    *,
    aggressive_mode: bool = False,
    price_exchange: str = C.PRICE_EXCHANGE,
    get_range_pct,
    get_momentum_over,
    get_one_hour_momentum,
    get_recent_high,
    get_recent_low,
) -> list[dict]:
    """Return scored pullback candidates that pass all entry filters."""
    candidates = []

    for sym in crypto_pairs:
        ticker = tickers.get(sym)
        if not ticker or ticker.get("last") is None:
            continue
        price = float(ticker["last"])

        rng = get_range_pct(db_conn, sym, C.VOL_WINDOW_MIN, price_exchange=price_exchange)
        if rng is None or rng < C.VOL_FLOOR_PCT:
            continue

        t3 = get_momentum_over(db_conn, sym, C.TREND_3H_MIN, price_exchange=price_exchange)
        t6 = get_momentum_over(db_conn, sym, C.TREND_6H_MIN, price_exchange=price_exchange)
        if t3 is None or t6 is None or t3 < C.TREND_3H_MIN_PCT or t6 <= 0:
            continue

        h1 = get_one_hour_momentum(db_conn, sym)
        if h1 is not None and h1 > C.BLOWOFF_GUARD_1H_PCT:
            continue

        hi1h = get_recent_high(db_conn, sym, 60, price_exchange=price_exchange)
        if hi1h is None or hi1h <= 0:
            continue
        pullback = (hi1h - price) / hi1h * 100.0
        if pullback < C.PULLBACK_MIN_PCT:
            continue

        hi6h = get_recent_high(db_conn, sym, 360, price_exchange=price_exchange)
        if hi6h is None or hi6h <= 0:
            continue
        room_pct = (hi6h - price) / price * 100.0

        if aggressive_mode:
            if room_pct <= 0:
                continue
        else:
            stop_dist = min(
                C.MAX_HARD_STOP_PCT,
                max(C.MIN_HARD_STOP_PCT, 0.5 * (rng or C.MIN_HARD_STOP_PCT * 2)),
            )
            if room_pct < C.RR_MIN * stop_dist:
                continue

        price_5m = get_momentum_over(db_conn, sym, 5, price_exchange=price_exchange)
        low15m = get_recent_low(db_conn, sym, 15, price_exchange=price_exchange)
        if (
            price_5m is not None
            and price_5m <= 0
            and low15m is not None
            and float(price) <= low15m * 1.001
        ):
            continue

        score = t3 + 0.5 * rng + pullback - max(0.0, (h1 or 0.0) - C.BLOWOFF_GUARD_1H_PCT)
        candidates.append({
            "symbol": sym,
            "price": price,
            "t3": t3,
            "t6": t6,
            "rng": rng,
            "pullback": pullback,
            "h1": h1,
            "score": score,
        })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates