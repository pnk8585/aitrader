"""Isolated virtual Kraken wallet projection for paper strategy cycles.

Paper execution must never inspect a live private balance.  Positions come
only from that paper strategy's already namespaced ``trading_state`` rows;
the configurable starting cash is a simulation budget, not an exchange value.
"""
from __future__ import annotations

import os


def paper_wallet_balance(state: dict, tickers: dict) -> dict:
    """Return a CCXT-shaped balance derived solely from paper state.

    The budget is intentionally conservative: amounts committed to open paper
    positions are unavailable for new entries.  No live account API is read.
    """
    try:
        starting_eur = float(os.getenv("PAPER_KRAKEN_STARTING_EUR", "1000"))
    except ValueError:
        starting_eur = 0.0
    starting_eur = max(0.0, starting_eur)
    total = {"EUR": starting_eur}
    committed = 0.0
    for symbol, position in state.items():
        qty = max(0.0, float(position.get("quantity") or 0.0))
        coin = symbol.split("/")[0]
        total[coin] = total.get(coin, 0.0) + qty
        committed += max(0.0, float(position.get("total_position_eur") or 0.0))
    free_eur = max(0.0, starting_eur - committed)
    return {"total": total, "free": {**total, "EUR": free_eur}, "used": {"EUR": committed}}
