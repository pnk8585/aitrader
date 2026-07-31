"""Shared stock/crypto universe — DB-backed with hardcoded defaults.

Bots call get_stock_symbols() / get_crypto_pairs() each cycle (30s cache).
Admin UI writes to universe_symbols; empty table falls back to defaults.
"""

from __future__ import annotations

import os
import time
from typing import Sequence

# ── Defaults (seed source + offline fallback) ──────────────────────────────

DEFAULT_STOCK_SYMBOLS: list[str] = [
    "NVDA", "PLTR", "TSLA", "AMD", "GOOGL", "META", "AAPL", "MSFT", "AMZN", "AVGO",
    "JPM", "XOM", "UNH", "CAT", "SPY",
]

DEFAULT_CRYPTO_PAIRS: list[str] = [
    "BTC/EUR", "ETH/EUR", "SOL/EUR", "AVAX/EUR", "LINK/EUR",
    "XRP/EUR", "DOGE/EUR", "SUI/EUR", "NEAR/EUR", "RENDER/EUR",
    "ADA/EUR", "DOT/EUR",
]

_TTL = 30.0
_cache: dict[str, tuple[float, list[str]]] = {}


def _normalize_stock(symbol: str) -> str:
    return symbol.strip().upper().replace(" ", "")


def _normalize_crypto(symbol: str) -> str:
    """Always BASE/EUR — system trades EUR pairs on Kraken."""
    s = symbol.strip().upper().replace(" ", "")
    if "/" in s:
        base = s.split("/", 1)[0]
    elif s.endswith("EUR") and len(s) > 3:
        base = s[:-3]
    else:
        base = s
    base = base.replace("/", "")
    if not base:
        raise ValueError("empty crypto base")
    return f"{base}/EUR"


def normalize_symbol(asset_class: str, symbol: str) -> str:
    if asset_class == "stock":
        return _normalize_stock(symbol)
    if asset_class == "crypto":
        return _normalize_crypto(symbol)
    raise ValueError(f"unknown asset_class: {asset_class}")


def _fetch_from_db(asset_class: str) -> list[str] | None:
    """Return enabled symbols from DB, or None if unavailable/empty."""
    try:
        import psycopg2
    except ImportError:
        return None

    host = os.getenv("DB_HOST")
    dbname = os.getenv("DB_NAME")
    user = os.getenv("DB_USER")
    if not (host and dbname and user):
        return None

    try:
        conn = psycopg2.connect(
            host=host,
            port=os.getenv("DB_PORT", "5432"),
            dbname=dbname,
            user=user,
            password=os.getenv("DB_PASSWORD", ""),
            connect_timeout=3,
        )
    except Exception:
        return None

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT symbol FROM universe_symbols "
                "WHERE asset_class = %s AND enabled = TRUE "
                "ORDER BY symbol",
                (asset_class,),
            )
            rows = [r[0] for r in cur.fetchall()]
        return rows if rows else None
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _load(asset_class: str, defaults: Sequence[str]) -> list[str]:
    now = time.monotonic()
    hit = _cache.get(asset_class)
    if hit and (now - hit[0]) < _TTL:
        return list(hit[1])

    from_db = _fetch_from_db(asset_class)
    result = list(from_db) if from_db is not None else list(defaults)
    _cache[asset_class] = (now, result)
    return list(result)


def invalidate_cache() -> None:
    _cache.clear()


def get_stock_symbols() -> list[str]:
    return _load("stock", DEFAULT_STOCK_SYMBOLS)


def get_crypto_pairs() -> list[str]:
    return _load("crypto", DEFAULT_CRYPTO_PAIRS)
