"""Strategy router: maps regime → active strategies."""

REGIME_STRATEGIES = {
    "trending":  {"momentum": True,  "pullback": True,  "grid": False},
    "ranging":   {"momentum": False, "pullback": False, "grid": True},
    "crisis":    {"momentum": False, "pullback": False, "grid": False},
    "uncertain": {"momentum": True,  "pullback": True,  "grid": False},
}

def get_active_strategies(regime: str) -> dict:
    """Returns which strategies should be active for this regime.
    Falls back to 'uncertain' for unknown regimes."""
    return REGIME_STRATEGIES.get(regime, REGIME_STRATEGIES["uncertain"])

def should_enter(db_conn, symbol: str, strategy: str) -> tuple:
    """Check if a strategy should enter for this symbol right now.
    Returns (allowed, reason). Fail-open on missing data."""
    try:
        # Clear any failed transaction state from prior errors in this cycle
        db_conn.rollback()
    except Exception:
        pass
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
