"""Wallet balance queries for the dashboard — Kraken (ccxt) + Alpaca (REST)."""
from __future__ import annotations

import os
from typing import Any

import requests

# ── Kraken ────────────────────────────────────────────────────

def _kraken_client():
    import ccxt
    return ccxt.kraken({
        "apiKey": os.getenv("KRAKEN_API_KEY", ""),
        "secret": os.getenv("KRAKEN_SECRET", ""),
        "enableRateLimit": True,
    })


def kraken_balance() -> dict[str, Any]:
    """Return {eur, positions_eur, total_eur, error}."""
    try:
        ex = _kraken_client()
        bal = ex.fetch_balance()
        total = bal.get("total", {})
        eur = float(total.get("EUR", 0) or 0)
        positions_eur = float(total.get("EUR", 0) or 0) - eur  # total EUR - free EUR ≈ locked
        # positions_eur is the locked/frozen EUR (reserved for open orders)
        locked_eur = float(bal.get("used", {}).get("EUR", 0) or 0) if "used" in bal else 0
        # For simpler display: total EUR = free + used (locked)
        free_eur = float(bal.get("free", {}).get("EUR", 0) or 0)
        total_eur = free_eur + locked_eur
        # Also include crypto held value by summing all non-EUR balances * last price
        return {
            "free_eur": round(free_eur, 2),
            "locked_eur": round(locked_eur, 2),
            "total_eur": round(total_eur, 2),
        }
    except Exception as e:
        return {"error": str(e)[:200]}


# ── Alpaca ────────────────────────────────────────────────────

def _alpaca_headers() -> dict:
    return {
        "APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", ""),
        "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", ""),
    }


def alpaca_balance() -> dict[str, Any]:
    """Return {cash, equity, positions_value, error}."""
    try:
        base = "https://paper-api.alpaca.markets" if os.getenv("AITRADER_MODE") == "paper" else "https://api.alpaca.markets"
        acc = requests.get(f"{base}/v2/account", headers=_alpaca_headers(), timeout=10)
        acc.raise_for_status()
        data = acc.json()
        cash = float(data.get("cash", 0))
        equity = float(data.get("equity", 0))
        positions_value = round(equity - cash, 2)
        return {
            "cash_eur": round(cash, 2),
            "equity_eur": round(equity, 2),
            "positions_eur": positions_value,
        }
    except Exception as e:
        return {"error": str(e)[:200]}
