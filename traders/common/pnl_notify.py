"""P&L notification suffix formatter for sell messages.

Appends a Greek-localised "Buy → Sell | P&L" suffix to trade notifications.
EUR amounts use €; USD amounts attempt EUR conversion via Kraken ticker
and fall back to labelled $ when the rate is unavailable.
"""

import math
import requests


def format_sell_pnl(entry_price, current_price, qty, *, currency="EUR"):
    """Return suffix like '| Αγορά €5.76 → Πώληση €5.89 | P&L +€0.13'.

    Returns empty string on non-finite prices, non-positive qty, or
    missing/invalid inputs so callers can leave messages unchanged.
    """
    sym = "€" if currency == "EUR" else "$"
    try:
        entry = float(entry_price)
        current = float(current_price)
        q = float(qty)
    except (TypeError, ValueError):
        return ""
    if not (math.isfinite(entry) and math.isfinite(current) and math.isfinite(q)):
        return ""
    if q <= 0 or entry <= 0 or current <= 0:
        return ""
    buy = entry * q
    sell = current * q
    pnl = sell - buy
    sign = "+" if pnl >= 0 else "−"  # U+2212 MINUS SIGN
    return f"| Αγορά {sym}{buy:.2f} → Πώληση {sym}{sell:.2f} | P&L {sign}{sym}{abs(pnl):.2f}"


def fetch_usd_to_eur_rate():
    """Fetch EUR/USD rate from Kraken public ticker. No API key needed.

    Returns the rate (USD per 1 EUR) or None on any failure. Never raises.
    """
    try:
        r = requests.get(
            "https://api.kraken.com/0/public/Ticker?pair=EURUSD",
            timeout=5,
        )
        data = r.json()
        last = float(data["result"]["ZEURZUSD"]["c"][0])
        return last
    except Exception:
        return None


def format_sell_pnl_auto(entry_price, current_price, qty, *, prices_in="EUR"):
    """Format P&L suffix, auto-converting USD prices to EUR when possible.

    prices_in="EUR": formats directly with €.
    prices_in="USD": fetches EUR/USD rate and converts to €; falls back to $
        when the rate is unavailable.
    """
    if prices_in == "EUR":
        return format_sell_pnl(entry_price, current_price, qty, currency="EUR")
    # USD path: try EUR conversion, fall back to labelled $
    rate = fetch_usd_to_eur_rate()
    if rate is not None and rate > 0:
        try:
            entry_eur = float(entry_price) / rate
            current_eur = float(current_price) / rate
            return format_sell_pnl(entry_eur, current_eur, float(qty), currency="EUR")
        except (TypeError, ValueError):
            return ""
    return format_sell_pnl(entry_price, current_price, qty, currency="USD")
