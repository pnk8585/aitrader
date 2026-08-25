"""Strategy router: maps regime → active strategies."""

from datetime import datetime, timezone, timedelta

from traders.strategies.regime.detector import detect_regime

REGIME_STRATEGIES = {
    "trending":  {"momentum": True,  "pullback": True,  "grid": False},
    "ranging":   {"momentum": False, "pullback": False, "grid": True},
    "crisis":    {"momentum": False, "pullback": False, "grid": False},
    # A data-poor regime is not a valid momentum signal.  Pullback keeps its
    # Phase-A behaviour, but momentum entries need an affirmative fresh regime.
    "uncertain": {"momentum": False, "pullback": True,  "grid": False},
}

# Recompute a symbol's regime if its latest regime_state row is older than this.
# Prevents the crisis deadlock (a stale 'crisis' row blocked entry forever)
# without re-writing regime_state for every symbol on every cycle.
REGIME_STALE_AFTER = timedelta(hours=4)


def get_active_strategies(regime: str) -> dict:
    """Returns which strategies should be active for this regime.
    Falls back to 'uncertain' for unknown regimes."""
    return REGIME_STRATEGIES.get(regime, REGIME_STRATEGIES["uncertain"])


def _regime_is_stale(db_conn, symbol: str) -> bool:
    """True if the latest regime_state row for `symbol` is older than the
    staleness threshold, including when no row exists (bootstrap required)."""
    cur = None
    try:
        cur = db_conn.cursor()
        cur.execute(
            """SELECT computed_at FROM regime_state
               WHERE symbol = %s ORDER BY computed_at DESC LIMIT 1""",
            (symbol,))
        row = cur.fetchone()
    finally:
        if cur is not None:
            cur.close()

    if not row:
        return True  # no row → perform one safe bootstrap refresh
    last = row[0]
    if last is None:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last) >= REGIME_STALE_AFTER


def _refresh_regime(db_conn, symbol: str) -> bool:
    """Recompute and persist the regime for ``symbol``; report success."""
    try:
        detect_regime(db_conn, symbol)
        return True
    except Exception:
        return False


def should_enter(db_conn, symbol: str, strategy: str) -> tuple:
    """Check if a strategy should enter for this symbol right now.
    Returns (allowed, reason). Momentum is intentionally fail-closed on
    missing, stale, or unreadable regime data; other strategies retain their
    established routing behaviour.

    Recomputes the regime first when the latest row is stale, so a coin that
    recovered from 'crisis' can re-enter instead of being frozen by an old row.
    """
    try:
        # Clear any failed transaction state from prior errors in this cycle
        db_conn.rollback()
    except Exception:
        pass

    # Recompute a stale regime BEFORE reading it (entry gate).
    refresh_ok = True
    try:
        if _regime_is_stale(db_conn, symbol):
            refresh_ok = _refresh_regime(db_conn, symbol)
    except Exception:
        refresh_ok = False
    if strategy == "momentum" and not refresh_ok:
        return False, "regime unavailable (refresh failed)"

    cur = None
    try:
        cur = db_conn.cursor()
        cur.execute(
            """SELECT regime FROM regime_state
               WHERE symbol = %s ORDER BY computed_at DESC LIMIT 1""",
            (symbol,))
        row = cur.fetchone()
    except Exception:
        if strategy == "momentum":
            return False, "regime unavailable (query failed)"
        return True, "regime query failed (allow by default)"
    finally:
        if cur is not None:
            cur.close()

    if not row:
        if strategy == "momentum":
            return False, "regime unavailable (no regime data)"
        return True, "no regime data (allow by default)"

    regime = row[0]
    active = get_active_strategies(regime)

    if active.get(strategy):
        return True, f"regime={regime}, {strategy} active"
    else:
        return False, f"regime={regime}, {strategy} inactive"
