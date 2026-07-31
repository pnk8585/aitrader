"""Admin-facing universe CRUD + Alpaca/Kraken symbol search."""

from __future__ import annotations

import os
import time
from typing import Any

import requests

from app.db import get_conn
from traders.common.universe import (
    DEFAULT_CRYPTO_PAIRS,
    DEFAULT_STOCK_SYMBOLS,
    invalidate_cache,
    normalize_symbol,
)

# Job names that scan each asset class (mode is job-level, not per-symbol)
STOCK_JOBS = ("alpaca-stocks",)
CRYPTO_JOBS = ("kraken-pullback", "kraken-momentum", "kraken-grid")

_kraken_markets_cache: tuple[float, list[dict[str, Any]]] | None = None
_alpaca_assets_cache: tuple[float, list[dict[str, Any]]] | None = None
_MARKETS_TTL = 600.0  # 10 min


def ensure_universe_seeded() -> None:
    """Insert defaults when universe_symbols is empty (idempotent)."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM universe_symbols")
        n = cur.fetchone()[0]
        if n > 0:
            return
        for sym in DEFAULT_STOCK_SYMBOLS:
            cur.execute(
                "INSERT INTO universe_symbols (asset_class, symbol, enabled) "
                "VALUES ('stock', %s, TRUE) ON CONFLICT DO NOTHING",
                (sym,),
            )
        for pair in DEFAULT_CRYPTO_PAIRS:
            cur.execute(
                "INSERT INTO universe_symbols (asset_class, symbol, enabled) "
                "VALUES ('crypto', %s, TRUE) ON CONFLICT DO NOTHING",
                (pair,),
            )
    invalidate_cache()


def list_symbols(asset_class: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT asset_class, symbol, enabled, created_at "
            "FROM universe_symbols WHERE asset_class = %s "
            "ORDER BY symbol",
            (asset_class,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def add_symbol(asset_class: str, symbol: str) -> dict[str, Any]:
    if asset_class not in ("stock", "crypto"):
        raise ValueError("asset_class must be stock or crypto")
    sym = normalize_symbol(asset_class, symbol)
    if asset_class == "stock":
        clean = sym.replace(".", "")
        if not (1 <= len(sym) <= 8 and clean.isalnum()):
            raise ValueError(f"invalid stock symbol: {sym}")
    else:
        if "/" not in sym or not sym.endswith("/EUR"):
            raise ValueError(f"crypto must be BASE/EUR, got: {sym}")
        base = sym.split("/", 1)[0]
        if not base.isalnum() or len(base) > 12:
            raise ValueError(f"invalid crypto base: {base}")

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO universe_symbols (asset_class, symbol, enabled) "
            "VALUES (%s, %s, TRUE) "
            "ON CONFLICT (asset_class, symbol) DO UPDATE SET enabled = TRUE "
            "RETURNING asset_class, symbol, enabled, created_at",
            (asset_class, sym),
        )
        row = cur.fetchone()
        cols = [d[0] for d in cur.description]
    invalidate_cache()
    return dict(zip(cols, row))


def remove_symbol(asset_class: str, symbol: str) -> bool:
    sym = normalize_symbol(asset_class, symbol)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM universe_symbols WHERE asset_class = %s AND symbol = %s",
            (asset_class, sym),
        )
        deleted = cur.rowcount > 0
    if deleted:
        invalidate_cache()
    return deleted


def job_modes() -> list[dict[str, Any]]:
    """Trading jobs relevant to universe + their live/paper mode."""
    names = list(STOCK_JOBS) + list(CRYPTO_JOBS)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT name, mode, enabled, schedule_seconds FROM cron_jobs "
            "WHERE name = ANY(%s) ORDER BY name",
            (names,),
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    # Fill missing jobs from registry defaults if seed not run yet
    have = {r["name"] for r in rows}
    try:
        from app.cron_orchestrator import JOB_REGISTRY
        for n in names:
            if n not in have and n in JOB_REGISTRY:
                _, secs, mode = JOB_REGISTRY[n]
                rows.append({
                    "name": n,
                    "mode": mode,
                    "enabled": True,
                    "schedule_seconds": secs,
                })
    except Exception:
        pass
    rows.sort(key=lambda r: r["name"])
    return rows


def _alpaca_headers() -> dict[str, str]:
    return {
        "APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", ""),
        "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", ""),
    }


def _alpaca_trading_base() -> str:
    from traders.common.config import ALPACA_BASE_URL
    return ALPACA_BASE_URL


def _load_alpaca_assets() -> list[dict[str, Any]]:
    global _alpaca_assets_cache
    now = time.monotonic()
    if _alpaca_assets_cache and (now - _alpaca_assets_cache[0]) < _MARKETS_TTL:
        return _alpaca_assets_cache[1]

    url = f"{_alpaca_trading_base()}/v2/assets"
    r = requests.get(
        url,
        headers=_alpaca_headers(),
        params={"status": "active", "asset_class": "us_equity"},
        timeout=20,
    )
    r.raise_for_status()
    assets = r.json()
    out = []
    for a in assets:
        if not a.get("tradable", True):
            continue
        out.append({
            "symbol": a.get("symbol", ""),
            "name": a.get("name") or a.get("symbol", ""),
            "exchange": a.get("exchange") or "",
            "asset_class": "stock",
        })
    _alpaca_assets_cache = (now, out)
    return out


def search_stocks(q: str, limit: int = 20) -> list[dict[str, Any]]:
    q = (q or "").strip().upper()
    if len(q) < 1:
        return []
    try:
        assets = _load_alpaca_assets()
    except Exception as e:
        return [{"symbol": "", "name": f"Alpaca search error: {e}", "error": True}]

    starts = [a for a in assets if a["symbol"].startswith(q)]
    start_set = {a["symbol"] for a in starts}
    contains = [
        a for a in assets
        if a["symbol"] not in start_set
        and (q in a["symbol"] or q in (a.get("name") or "").upper())
    ]
    # Prefer exact / prefix matches
    ranked = starts + contains
    return ranked[:limit]


def _load_kraken_markets() -> list[dict[str, Any]]:
    global _kraken_markets_cache
    now = time.monotonic()
    if _kraken_markets_cache and (now - _kraken_markets_cache[0]) < _MARKETS_TTL:
        return _kraken_markets_cache[1]

    import ccxt
    ex = ccxt.kraken({"enableRateLimit": True})
    markets = ex.load_markets()
    out = []
    for key, m in markets.items():
        if not m.get("active", True):
            continue
        quote = (m.get("quote") or "").upper()
        # Prefer EUR pairs for this system
        if quote not in ("EUR", "USD", "USDT"):
            continue
        out.append({
            "symbol": m.get("symbol") or key,
            "name": f"{m.get('base', '')}/{quote}",
            "quote": quote,
            "exchange": "kraken",
            "asset_class": "crypto",
        })
    # EUR first in ranking later
    _kraken_markets_cache = (now, out)
    return out


def search_crypto(q: str, limit: int = 20) -> list[dict[str, Any]]:
    raw = (q or "").strip().upper().replace(" ", "")
    if len(raw) < 1:
        return []
    try:
        markets = _load_kraken_markets()
    except Exception as e:
        return [{"symbol": "", "name": f"Kraken search error: {e}", "error": True}]

    # Normalize query fragments
    q_base = raw.split("/")[0].replace("EUR", "").replace("USD", "") or raw

    def score(m: dict) -> tuple:
        sym = m["symbol"].upper()
        base = sym.split("/")[0]
        quote = m.get("quote") or ""
        exact = 0 if base == q_base or sym == raw else 1
        eur = 0 if quote == "EUR" else 1
        prefix = 0 if base.startswith(q_base) or sym.startswith(raw) else 1
        return (exact, eur, prefix, sym)

    matched = [
        m for m in markets
        if q_base in m["symbol"].upper() or raw in m["symbol"].upper()
        or q_base in (m.get("name") or "").upper()
    ]
    matched.sort(key=score)
    # Prefer EUR-only hits when available (system normalizes adds to BASE/EUR)
    eur = [m for m in matched if m.get("quote") == "EUR"]
    if eur:
        matched = eur + [m for m in matched if m not in eur]
    return matched[:limit]
