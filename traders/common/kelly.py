"""Kelly Criterion position sizing based on trade history."""


def kelly_fraction(win_rate, avg_win, avg_loss):
    """Raw Kelly fraction: f = win_rate - (1 - win_rate) / (avg_win / avg_loss).

    Returns 0.0 if inputs are invalid (no edge).
    """
    if avg_loss == 0 or avg_win <= 0 or avg_loss >= 0:
        return 0.0
    rr = abs(avg_win / avg_loss)
    return win_rate - ((1 - win_rate) / rr)


def kelly_position_size(db_conn, exchange_name, entry, stop, balance, fraction=0.25):
    """Compute position size in quote currency using quarter-Kelly.

    Reads last 200 trades from trade_log to estimate win rate and R:R.
    Falls back to fixed fraction of balance if fewer than 100 historical trades.
    Caps at quarter-Kelly.
    """
    # These values define whether the trade can exist at all.  Validate before
    # reading history so the small-sample fallback cannot allocate to invalid
    # geometry or a nonpositive account/configuration value.
    if entry <= 0 or stop >= entry or balance <= 0 or fraction <= 0:
        return 0.0

    cur = db_conn.cursor()
    cur.execute(
        """SELECT action, unrealized_plpc
           FROM trade_log
           WHERE exchange = %s AND action = 'SELL'
           ORDER BY id DESC
           LIMIT 200""",
        (exchange_name,),
    )
    rows = cur.fetchall()
    cur.close()

    if len(rows) < 100:
        return balance * fraction

    wins = [r[1] for r in rows if r[1] and r[1] > 0]
    losses = [r[1] for r in rows if r[1] and r[1] < 0]
    if not wins or not losses:
        return balance * fraction

    win_rate = len(wins) / len(rows)
    avg_win = sum(wins) / len(wins)
    avg_loss = sum(losses) / len(losses)

    kf = kelly_fraction(win_rate, avg_win, avg_loss)
    kf = max(0.0, min(kf, fraction))

    # A measured non-positive edge is a decision to allocate nothing, not a
    # reason to silently resume the small-sample fixed allocation.
    if kf <= 0 or stop >= entry:
        return 0.0

    risk_per_unit = abs(entry - stop) / entry
    if risk_per_unit <= 0:
        return 0.0

    return balance * kf
