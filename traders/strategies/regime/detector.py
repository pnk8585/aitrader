"""Rules-based regime detection from asset_prices.

Returns one of: 'trending' | 'ranging' | 'crisis' | 'uncertain'
"""

from traders.strategies.regime import config as C


def detect_regime(db_conn, symbol):
    """Classify market regime for a symbol using ADX(14), 20d vol, 20d return.

    Reads from asset_prices (symbol, price, timestamp, exchange='kraken').
    """
    cur = db_conn.cursor()
    cur.execute(
        """SELECT price, timestamp
           FROM asset_prices
           WHERE symbol = %s AND exchange = 'kraken'
           ORDER BY timestamp DESC
           LIMIT %s""",
        (symbol, C.VOL_WINDOW * 2),
    )
    rows = cur.fetchall()
    cur.close()

    if len(rows) < C.ADX_PERIOD + 1:
        return "uncertain"

    prices = [float(r[0]) for r in reversed(rows)]

    adx = _approx_adx(prices, C.ADX_PERIOD)
    vol20 = _volatility(prices, C.VOL_WINDOW)
    ret20 = _return_pct(prices, C.RET_WINDOW)

    regime = "uncertain"

    if adx is None:
        regime = "uncertain"
    elif vol20 is not None and vol20 >= C.VOL_CRISIS_THRESHOLD:
        regime = "crisis"
    elif adx >= C.ADX_TREND_THRESHOLD:
        regime = "trending"
    elif adx <= C.ADX_RANGE_THRESHOLD and vol20 is not None and vol20 <= C.VOL_RANGE_THRESHOLD:
        regime = "ranging"
    elif ret20 is not None and abs(ret20) >= C.RET_TREND_THRESHOLD:
        regime = "trending"

    cur = db_conn.cursor()
    cur.execute(
        """INSERT INTO regime_state (symbol, regime, adx_14, vol_20d, ret_20d)
           VALUES (%s, %s, %s, %s, %s)""",
        (symbol, regime,
         round(adx, 2) if adx else None,
         round(vol20, 2) if vol20 else None,
         round(ret20, 2) if ret20 else None),
    )
    cur.close()

    return regime


def _approx_adx(prices, period):
    """Approximate ADX from close prices only.

    Uses close-to-close directional movement as a proxy for true +DM/-DM.
    """
    if len(prices) < period + 1:
        return None
    trs = []
    plus_dm = []
    minus_dm = []
    for i in range(1, len(prices)):
        up = prices[i] - prices[i - 1]
        trs.append(abs(up))
        plus_dm.append(up if up > 0 else 0)
        minus_dm.append(abs(up) if up < 0 else 0)

    atr = sum(trs[:period]) / period
    atr_smooth = atr
    pdm_smooth = sum(plus_dm[:period]) / period
    mdm_smooth = sum(minus_dm[:period]) / period
    for i in range(period, len(trs)):
        atr_smooth = (atr_smooth * (period - 1) + trs[i]) / period
        pdm_smooth = (pdm_smooth * (period - 1) + plus_dm[i]) / period
        mdm_smooth = (mdm_smooth * (period - 1) + minus_dm[i]) / period

    if atr_smooth == 0:
        return 0.0
    pdi = pdm_smooth / atr_smooth * 100
    mdi = mdm_smooth / atr_smooth * 100
    dx = abs(pdi - mdi) / (pdi + mdi) * 100 if (pdi + mdi) > 0 else 0
    return dx


def _volatility(prices, window):
    """Annualized volatility over the last `window` periods, in percent.

    Prices are ~5-min intervals. Scale period returns to daily first,
    then annualize: daily = period * sqrt(288), annual = daily * sqrt(365).
    """
    if len(prices) < window + 1:
        return None
    segment = prices[-window - 1:]
    returns = [(segment[i] - segment[i - 1]) / segment[i - 1] for i in range(1, len(segment))]
    mean_r = sum(returns) / len(returns)
    var = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
    period_vol = var ** 0.5
    # 288 five-minute periods per day, 365 days per year
    daily_vol = period_vol * (288 ** 0.5)
    return daily_vol * 100 * (365 ** 0.5)


def _return_pct(prices, window):
    """Percent return over the last `window` periods."""
    if len(prices) < window:
        return None
    return (prices[-1] - prices[-window]) / prices[-window] * 100
