"""Strategy router: maps regime → active strategies."""

from datetime import datetime, timezone, timedelta

from traders.strategies.regime.detector import detect_regime

REGIME_STRATEGIES = {
    "trending":  {"momentum": True,  "pullback": True,  "grid": False},
    "ranging":   {"momentum": False, "pullback": False, "grid": True},
    "crisis":    {"momentum": False, "pullback": False, "grid": False},
    "uncertain": {"momentum": True,  "pullback": True,  "grid": False},
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
    staleness threshold. False when there is no row (fail-open)."""
    cur = db_conn.cursor()
    try:
        cur.execute(
            """SELECT computed_at FROM regime_state
               WHERE symbol = %s ORDER BY computed_at DESC LIMIT 1""",
            (symbol,))
        row = cur.fetchone()
    finally:
        cur.close()

    if not row:
        return False  # no row → should_enter allows by default
    last = row[0]
    if last is None:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last) >= REGIME_STALE_AFTER


def _refresh_regime(db_conn, symbol: str) -> None:
    """Recompute and persist the regime for `symbol`. Never raises."""
    try:
        detect_regime(db_conn, symbol)
    except Exception:
        pass  # fail-open: a refresh failure must not block the entry check


def should_enter(db_conn, symbol: str, strategy: str) -> tuple:
    """Check if a strategy should enter for this symbol right now.
    Returns (allowed, reason). Fail-open on missing data.

    Recomputes the regime first when the latest row is stale, so a coin that
    recovered from 'crisis' can re-enter instead of being frozen by an old row.
    """
    try:
        # Clear any failed transaction state from prior errors in this cycle
        db_conn.rollback()
    except Exception:
        pass

    # Recompute a stale regime BEFORE reading it (entry gate).
    try:
        if _regime_is_stale(db_conn, symbol):
            _refresh_regime(db_conn, symbol)
    except Exception:
        pass  # fail-open: never let the staleness check block an entry check

    cur = db_conn.cursor()
    try:
        cur.execute(
            """SELECT regime FROM regime_state
               WHERE symbol = %s ORDER BY computed_at DESC LIMIT 1""",
            (symbol,))
        row = cur.fetchone()
    except Exception:
        return True, "regime query failed (allow by default)"
    finally:
        cur.close()

    if not row:
        return True, "no regime data (allow by default)"

    regime = row[0]
    active = get_active_strategies(regime)

    if active.get(strategy):
        return True, f"regime={regime}, {strategy} active"
    else:
        return False, f"regime={regime}, {strategy} inactive"
