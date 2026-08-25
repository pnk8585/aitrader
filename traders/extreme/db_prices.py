import os
import sys
import math
import statistics
import time
from datetime import datetime, timedelta, timezone
import psycopg2
import psycopg2.extras
from psycopg2.extras import execute_values
from dotenv import load_dotenv

# Load environment variables
ROOT_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))
env_path = os.path.join(ROOT_DIR, ".env")
load_dotenv(dotenv_path=env_path)

EXCHANGE = "kraken"
DEBUG = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")

LOSS_STREAK_COUNT = 3
LOSS_STREAK_LOOKBACK_HOURS = 24
LOSS_STREAK_COOLDOWN_HOURS = 6
ROUND_TRIP_FEE_DECIMAL = 0.0052


def _safe_rollback(conn):
    """Rollback a failed transaction so subsequent queries can proceed."""
    try:
        conn.rollback()
    except Exception:
        pass


def base_symbol(pair):
    """Extract base coin from a CCXT pair, e.g. 'BTC/EUR' -> 'BTC'."""
    return pair.split('/')[0].upper()


def get_connection():
    """Open a psycopg2 connection to local postgres using .env config.

    Reads DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD. Uses a short
    connect_timeout so the caller never blocks for long. On any failure
    returns None instead of raising, so price logging stays best-effort.
    Ensures the schema exists before returning.
    """
    try:
        conn = psycopg2.connect(
            host=os.getenv("DB_HOST", "localhost"),
            port=os.getenv("DB_PORT", "5432"),
            dbname=os.getenv("DB_NAME"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            connect_timeout=5,
        )
        conn.autocommit = True  # Each statement auto-commits; no transaction accumulation
    except Exception as e:
        if DEBUG:
            raise
        print(f"DB connection failed: {e}", file=sys.stderr)
        return None

    try:
        ensure_schema(conn)
    except Exception as e:
        if DEBUG:
            raise
        print(f"Schema bootstrap failed: {e}", file=sys.stderr)
        close_connection(conn)
        return None

    return conn


def ensure_schema(conn):
    """Idempotently create all tables used by the trading engine."""
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS asset_prices (
                id SERIAL PRIMARY KEY,
                timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                exchange VARCHAR(50),
                symbol VARCHAR(50),
                price NUMERIC(20,10)
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_asset_prices_query
            ON asset_prices (exchange, symbol, timestamp DESC)
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS trading_state (
                id SERIAL PRIMARY KEY,
                exchange VARCHAR(50) NOT NULL,
                symbol VARCHAR(50) NOT NULL,
                entry_price NUMERIC,
                entry_time TIMESTAMP WITH TIME ZONE,
                peak_plpc NUMERIC DEFAULT 0,
                quantity NUMERIC DEFAULT 0,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # Migration for tables created before the quantity column existed.
        cur.execute(
            "ALTER TABLE trading_state ADD COLUMN IF NOT EXISTS quantity NUMERIC DEFAULT 0"
        )
        cur.execute(
            "ALTER TABLE trading_state ADD COLUMN IF NOT EXISTS dca_level INT DEFAULT 0"
        )
        cur.execute(
            "ALTER TABLE trading_state ADD COLUMN IF NOT EXISTS signal_price NUMERIC"
        )
        cur.execute(
            "ALTER TABLE trading_state ADD COLUMN IF NOT EXISTS total_position_eur NUMERIC"
        )
        cur.execute(
            "ALTER TABLE trading_state ADD COLUMN IF NOT EXISTS tp_level INT DEFAULT 0"
        )
        cur.execute(
            "ALTER TABLE trading_state ADD COLUMN IF NOT EXISTS tp_sold_qty NUMERIC DEFAULT 0"
        )
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS trading_state_exchange_symbol_key
            ON trading_state (exchange, symbol)
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS regime_state (
                id SERIAL PRIMARY KEY,
                symbol VARCHAR(20) NOT NULL,
                regime VARCHAR(20),
                adx_14 NUMERIC,
                vol_20d NUMERIC,
                ret_20d NUMERIC,
                computed_at TIMESTAMPTZ DEFAULT NOW()
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_regime_symbol ON regime_state (symbol, computed_at DESC)"
        )
        cur.execute(
            "ALTER TABLE regime_state ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW()"
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS grid_state (
                id SERIAL PRIMARY KEY,
                symbol TEXT NOT NULL,
                exchange TEXT NOT NULL DEFAULT 'kraken',
                grid_low NUMERIC NOT NULL,
                grid_high NUMERIC NOT NULL,
                num_grids INTEGER NOT NULL DEFAULT 10,
                capital_allocated NUMERIC NOT NULL,
                levels JSONB NOT NULL DEFAULT '[]',
                total_buys INTEGER DEFAULT 0,
                total_sells INTEGER DEFAULT 0,
                realized_pnl NUMERIC DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(symbol, exchange)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_grid_state_symbol ON grid_state(symbol, exchange)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_grid_state_status ON grid_state(status)"
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS notify_state (
                id SERIAL PRIMARY KEY,
                exchange VARCHAR(50) NOT NULL,
                last_notify_time TIMESTAMP WITH TIME ZONE
                    DEFAULT '1970-01-01 00:00:00+00'::TIMESTAMP WITH TIME ZONE,
                extra JSONB DEFAULT '{}'::JSONB,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS notify_state_exchange_key
            ON notify_state (exchange)
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS trade_log (
                id SERIAL PRIMARY KEY,
                timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                exchange VARCHAR(50) NOT NULL,
                action VARCHAR(50) NOT NULL,
                ticker VARCHAR(50),
                signal_strength VARCHAR(50),
                momentum_pct NUMERIC,
                entry_price NUMERIC,
                current_price NUMERIC,
                unrealized_plpc NUMERIC,
                order_id VARCHAR(100),
                client_order_id VARCHAR(100),
                quantity NUMERIC,
                estimated_value NUMERIC,
                position_size_pct NUMERIC,
                portfolio_equity NUMERIC,
                reason TEXT,
                regime VARCHAR(20),
                atr_at_entry NUMERIC(20,10),
                kelly_fraction NUMERIC(10,6),
                strategy_name VARCHAR(50)
            )
            """
        )
        cur.execute(
            "ALTER TABLE trade_log ADD COLUMN IF NOT EXISTS regime VARCHAR(20)"
        )
        cur.execute(
            "ALTER TABLE trade_log ADD COLUMN IF NOT EXISTS atr_at_entry NUMERIC(20,10)"
        )
        cur.execute(
            "ALTER TABLE trade_log ADD COLUMN IF NOT EXISTS kelly_fraction NUMERIC(10,6)"
        )
        cur.execute(
            "ALTER TABLE trade_log ADD COLUMN IF NOT EXISTS strategy_name VARCHAR(50)"
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_trade_log_ts
            ON trade_log (timestamp DESC)
            """
        )
    conn.commit()


def insert_prices(conn, price_map):
    """Bulk insert current Kraken prices.

    price_map maps a symbol (pair like 'BTC/EUR' or base like 'BTC') to a
    numeric price. Symbols are normalised to their base coin before insert.
    Best-effort: returns the number of rows written, 0 on failure or no conn.
    """
    if conn is None or not price_map:
        return 0

    rows = []
    for sym, price in price_map.items():
        if price is None:
            continue
        rows.append((EXCHANGE, base_symbol(sym), price))

    if not rows:
        return 0

    try:
        with conn.cursor() as cur:
            execute_values(
                cur,
                "INSERT INTO asset_prices (exchange, symbol, price) VALUES %s",
                rows,
            )
        conn.commit()
        return len(rows)
    except Exception as e:
        if DEBUG:
            raise
        print(f"insert_prices failed: {e}", file=sys.stderr)
        try:
            conn.rollback()
        except Exception:
            pass
        return 0


def get_one_hour_momentum(conn, symbol):
    """Return the % price change over roughly the last hour for a coin.

    Compares the latest price against the price closest to 60 minutes ago,
    looking only inside a 55-75 minute window to avoid stale comparisons.
    Returns None when no connection, no recent price, or no in-window past
    price exists.
    """
    if conn is None:
        return None

    base = base_symbol(symbol)

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT price
                FROM asset_prices
                WHERE exchange = %s AND symbol = %s
                ORDER BY timestamp DESC
                LIMIT 1
                """,
                (EXCHANGE, base),
            )
            latest = cur.fetchone()
            if latest is None or latest[0] is None:
                return None
            latest_price = float(latest[0])

            cur.execute(
                """
                SELECT price
                FROM asset_prices
                WHERE exchange = %s AND symbol = %s
                  AND timestamp <= CURRENT_TIMESTAMP - INTERVAL '55 minutes'
                  AND timestamp >= CURRENT_TIMESTAMP - INTERVAL '75 minutes'
                ORDER BY ABS(EXTRACT(EPOCH FROM (
                    timestamp - (CURRENT_TIMESTAMP - INTERVAL '60 minutes')
                )))
                LIMIT 1
                """,
                (EXCHANGE, base),
            )
            past = cur.fetchone()
            if past is None or past[0] is None:
                return None
            past_price = float(past[0])
    except Exception as e:
        if DEBUG:
            raise
        _safe_rollback(conn)
        print(f"get_one_hour_momentum failed: {e}", file=sys.stderr)
        return None

    if past_price == 0:
        return None

    return (latest_price - past_price) / past_price * 100


def close_connection(conn):
    """Safely close a psycopg2 connection."""
    if conn is None:
        return
    try:
        conn.close()
    except Exception as e:
        if DEBUG:
            raise
        print(f"close_connection failed: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Shared market-read helpers (used by both pullback & momentum strategies)
# ---------------------------------------------------------------------------

MOMENTUM_TARGET_TOLERANCE = 0.15
MOMENTUM_LATEST_MAX_AGE_MINUTES = 15
MOMENTUM_NEIGHBOR_CONSISTENCY = 0.20


def get_momentum_snapshot(conn, symbol, minutes, price_exchange="kraken"):
    """Return validated latest/target prices for a timestamp-aware momentum read.

    The target must be within +/-15% of its requested age.  The latest sample
    must be no more than 15 minutes old.  A target is rejected when it differs
    by more than 20% from the robust median of nearby samples: this detects a
    lone bad persisted tick while allowing a coherent breakout whose neighbours
    move with it.  These are data-quality checks, not a cap on market returns.
    """
    if conn is None:
        return None
    base = base_symbol(symbol)
    older = int(minutes * (1 + MOMENTUM_TARGET_TOLERANCE))
    newer = int(minutes * (1 - MOMENTUM_TARGET_TOLERANCE))
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT price, timestamp FROM asset_prices WHERE exchange=%s AND symbol=%s "
                "ORDER BY timestamp DESC LIMIT 1", (price_exchange, base))
            latest = cur.fetchone()
            if not latest or latest[0] is None or latest[1] is None:
                return None
            latest_price, latest_ts = float(latest[0]), latest[1]
            if not math.isfinite(latest_price) or latest_price <= 0:
                return None
            if latest_ts.tzinfo is None:
                latest_ts = latest_ts.replace(tzinfo=timezone.utc)
            latest_age = datetime.now(timezone.utc) - latest_ts
            if latest_age < timedelta(minutes=-1) or latest_age > timedelta(minutes=MOMENTUM_LATEST_MAX_AGE_MINUTES):
                return None
            cur.execute(
                "SELECT price, timestamp FROM asset_prices WHERE exchange=%s AND symbol=%s "
                "AND timestamp <= CURRENT_TIMESTAMP - make_interval(mins => %s) "
                "AND timestamp >= CURRENT_TIMESTAMP - make_interval(mins => %s) "
                "ORDER BY abs(EXTRACT(EPOCH FROM (timestamp - (CURRENT_TIMESTAMP - make_interval(mins => %s))))) LIMIT 1",
                (price_exchange, base, newer, older, minutes))
            past = cur.fetchone()
            if not past or past[0] is None or past[1] is None:
                return None
            past_price, past_ts = float(past[0]), past[1]
            if not math.isfinite(past_price) or past_price <= 0:
                return None
            if past_ts.tzinfo is None:
                past_ts = past_ts.replace(tzinfo=timezone.utc)
            target_age = datetime.now(timezone.utc) - past_ts
            if not timedelta(minutes=newer) <= target_age <= timedelta(minutes=older):
                return None
            cur.execute(
                "SELECT price, timestamp FROM asset_prices WHERE exchange=%s AND symbol=%s "
                "AND timestamp BETWEEN %s - INTERVAL '30 minutes' AND %s + INTERVAL '30 minutes' "
                "ORDER BY abs(EXTRACT(EPOCH FROM (timestamp - %s))) LIMIT 5",
                (price_exchange, base, past_ts, past_ts, past_ts))
            neighbours = cur.fetchall()
    except Exception as e:
        if DEBUG:
            raise
        _safe_rollback(conn)
        print(f"get_momentum_over failed: {e}", file=sys.stderr)
        return None
    if any(not row or row[0] is None or not math.isfinite(float(row[0])) or float(row[0]) <= 0
           for row in neighbours):
        return None
    valid_neighbours = [float(row[0]) for row in neighbours
                        if len(row) > 1 and row[1] != past_ts]
    if len(valid_neighbours) >= 2:
        median = statistics.median(valid_neighbours)
        if abs(past_price - median) / median > MOMENTUM_NEIGHBOR_CONSISTENCY:
            return None
    return {"latest_price": latest_price, "latest_timestamp": latest_ts,
            "target_price": past_price, "target_timestamp": past_ts}


def get_momentum_over(conn, symbol, minutes, price_exchange="kraken"):
    """Public compatibility wrapper: percent change or ``None``."""
    snapshot = get_momentum_snapshot(conn, symbol, minutes, price_exchange)
    if snapshot is None:
        return None
    return (snapshot["latest_price"] - snapshot["target_price"]) / snapshot["target_price"] * 100.0


def market_data_sane(conn, symbol, observed_price, *, price_exchange=EXCHANGE,
                     max_age_minutes=15, max_deviation_pct=2.0):
    """Validate a ticker against a fresh, timestamped persisted DB sample.

    Entry paths must not turn a stale database quote or a material disagreement
    between the exchange ticker and stored price history into a momentum BUY.
    The preceding row is from the same Kraken venue, not an independent or
    cross-source feed; it is only a persisted-observation consistency check.
    Fail closed on unavailable data; this helper is deliberately read-only.
    """
    if conn is None or observed_price is None:
        return False, "price sanity unavailable"
    try:
        observed_price = float(observed_price)
    except (TypeError, ValueError, OverflowError):
        return False, "price sanity unavailable"
    if not math.isfinite(observed_price) or observed_price <= 0:
        return False, "price sanity unavailable"
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT price, timestamp FROM asset_prices "
                "WHERE exchange=%s AND symbol=%s ORDER BY timestamp DESC LIMIT 1",
                (price_exchange, base_symbol(symbol)))
            row = cur.fetchone()
    except Exception as e:
        _safe_rollback(conn)
        print(f"market_data_sane failed: {e}", file=sys.stderr)
        return False, "price sanity unavailable"
    if not row or row[0] is None or row[1] is None:
        return False, "price sanity unavailable"
    try:
        db_price = float(row[0])
    except (TypeError, ValueError, OverflowError):
        return False, "price sanity unavailable"
    timestamp = row[1]
    if not math.isfinite(db_price) or db_price <= 0:
        return False, "price sanity unavailable"
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - timestamp
    if age > timedelta(minutes=max_age_minutes) or age < timedelta(minutes=-1):
        return False, "price sanity stale"
    divergence = abs(observed_price - db_price) / db_price * 100.0
    if divergence > max_deviation_pct:
        return False, f"price sanity divergence {divergence:.2f}%"
    return True, ""


def get_range_pct(conn, symbol, minutes, price_exchange="kraken"):
    """Hi-lo range (%) over the last `minutes`. None if too little history."""
    if conn is None:
        return None
    base = base_symbol(symbol)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT MIN(price), MAX(price), COUNT(*) FROM asset_prices "
                "WHERE exchange=%s AND symbol=%s "
                "AND timestamp >= CURRENT_TIMESTAMP - make_interval(mins => %s)",
                (price_exchange, base, minutes))
            row = cur.fetchone()
    except Exception as e:
        if DEBUG:
            raise
        _safe_rollback(conn)
        print(f"get_range_pct failed: {e}", file=sys.stderr)
        return None
    if not row or row[0] is None or row[2] < 6:
        return None
    lo, hi = float(row[0]), float(row[1])
    if lo == 0:
        return None
    return (hi - lo) / lo * 100.0


def get_recent_high(conn, symbol, minutes, price_exchange="kraken"):
    """Highest price over the last `minutes`. None if no history."""
    if conn is None:
        return None
    base = base_symbol(symbol)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT MAX(price) FROM asset_prices WHERE exchange=%s AND symbol=%s "
                "AND timestamp >= CURRENT_TIMESTAMP - make_interval(mins => %s)",
                (price_exchange, base, minutes))
            row = cur.fetchone()
    except Exception as e:
        if DEBUG:
            raise
        print(f"get_recent_high failed: {e}", file=sys.stderr)
        return None
    if not row or row[0] is None:
        return None
    return float(row[0])


def get_recent_low(conn, symbol, minutes, price_exchange="kraken"):
    """Lowest price over the last `minutes`. None if no history."""
    if conn is None:
        return None
    base = base_symbol(symbol)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT MIN(price) FROM asset_prices WHERE exchange=%s AND symbol=%s "
                "AND timestamp >= CURRENT_TIMESTAMP - make_interval(mins => %s)",
                (price_exchange, base, minutes))
            row = cur.fetchone()
    except Exception as e:
        if DEBUG:
            raise
        print(f"get_recent_low failed: {e}", file=sys.stderr)
        return None
    if not row or row[0] is None:
        return None
    return float(row[0])


def last_exit_time(conn, symbol, exchange_name):
    """Timestamp of the most recent SELL for this coin (this strategy), for cooldown."""
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT MAX(timestamp) FROM trade_log WHERE exchange=%s "
                "AND ticker=%s AND action='SELL'", (exchange_name, symbol))
            row = cur.fetchone()
    except Exception as e:
        if DEBUG:
            raise
        _safe_rollback(conn)
        print(f"last_exit_time failed: {e}", file=sys.stderr)
        return None
    return row[0] if row else None


def trades_today(conn, exchange_name):
    """Count of BUYs since 00:00 UTC for the daily overtrading cap."""
    if conn is None:
        return 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM trade_log WHERE exchange=%s AND action='BUY' "
                "AND timestamp >= date_trunc('day', CURRENT_TIMESTAMP AT TIME ZONE 'UTC')",
                (exchange_name,))
            return int(cur.fetchone()[0])
    except Exception as e:
        if DEBUG:
            raise
        print(f"trades_today failed: {e}", file=sys.stderr)
        return 0


def realized_pnl_today_pct(conn, exchange_name, round_trip_fee_pct):
    """Approx realized PnL today (%) for the daily loss circuit-breaker.

    Sum of today's SELL unrealized_plpc minus round-trip fees.
    """
    if conn is None:
        return 0.0
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(unrealized_plpc),0), COUNT(*) FROM trade_log "
                "WHERE exchange=%s AND action='SELL' "
                "AND timestamp >= date_trunc('day', CURRENT_TIMESTAMP AT TIME ZONE 'UTC')",
                (exchange_name,))
            row = cur.fetchone()
    except Exception as e:
        if DEBUG:
            raise
        print(f"realized_pnl_today_pct failed: {e}", file=sys.stderr)
        return 0.0
    gross_pct = float(row[0]) * 100.0
    n = int(row[1])
    return gross_pct - n * round_trip_fee_pct


def loss_streak_cooldown(conn, exchange_name, *, consecutive_losses=LOSS_STREAK_COUNT,
                         lookback_hours=LOSS_STREAK_LOOKBACK_HOURS,
                         cooldown_hours=LOSS_STREAK_COOLDOWN_HOURS):
    """Return ``(blocked, reason)`` for new BUYs in exactly one strategy.

    ``trade_log.unrealized_plpc`` is a decimal DB fraction, so estimated
    round-trip fees are subtracted as 0.0052 (not 0.52 or 0.0052%).
    Query/connection failures intentionally fail closed for entry paths.
    """
    if conn is None:
        return True, "Loss-streak cooldown unavailable (DB connection missing)."
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT timestamp, unrealized_plpc FROM trade_log
                   WHERE exchange=%s AND action='SELL'
                     AND timestamp >= CURRENT_TIMESTAMP - make_interval(hours => %s)
                   ORDER BY timestamp DESC LIMIT %s""",
                (exchange_name, lookback_hours, consecutive_losses),
            )
            rows = cur.fetchall()
    except Exception as e:
        _safe_rollback(conn)
        print(f"loss_streak_cooldown failed: {e}", file=sys.stderr)
        return True, "Loss-streak cooldown unavailable (DB query failed)."
    if len(rows) < consecutive_losses:
        return False, ""
    if not all(float(row[1] or 0.0) - ROUND_TRIP_FEE_DECIMAL < 0.0 for row in rows):
        return False, ""
    latest = rows[0][0]
    if latest is None:
        return False, ""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=cooldown_hours)
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    if latest >= cutoff:
        return True, (f"Loss-streak cooldown: {consecutive_losses} consecutive net losing SELLs; "
                      f"new BUYs blocked for {cooldown_hours}h.")
    return False, ""


# ---------------------------------------------------------------------------
# Trading State (αντικαθιστά state.json / kraken_state.json)
# ---------------------------------------------------------------------------

def load_trading_state(conn, exchange):
    """Load all positions for an exchange as a dict {symbol: {entry_price, entry_time, peak_plpc, quantity}}."""
    if conn is None:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT symbol, entry_price, entry_time, peak_plpc, quantity, "
                "dca_level, signal_price, total_position_eur, "
                "tp_level, tp_sold_qty "
                "FROM trading_state WHERE exchange = %s",
                (exchange,),
            )
            rows = cur.fetchall()
        state = {}
        for symbol, ep, et, peak, qty, dca_lvl, sig_px, tot_eur, tp_lvl, tp_sold in rows:
            state[symbol] = {
                "entry_price": float(ep) if ep else 0.0,
                "entry_time": et.isoformat().replace("+00:00", "Z") if et else None,
                "peak_plpc": float(peak) if peak else 0.0,
                "quantity": float(qty) if qty else 0.0,
                "dca_level": int(dca_lvl) if dca_lvl is not None else 0,
                "signal_price": float(sig_px) if sig_px else 0.0,
                "total_position_eur": float(tot_eur) if tot_eur else 0.0,
                "tp_level": int(tp_lvl) if tp_lvl is not None else 0,
                "tp_sold_qty": float(tp_sold) if tp_sold else 0.0,
            }
        return state
    except Exception as e:
        if DEBUG:
            raise
        print(f"load_trading_state failed: {e}", file=sys.stderr)
        return {}


def save_trading_state(conn, exchange, state):
    """Persist all positions for an exchange via UPSERT, then prune rows no
    longer held. state = {symbol: {entry_price, entry_time, peak_plpc, quantity}}.

    Uses ON CONFLICT upsert (not DELETE-all + INSERT) so a crash mid-write can
    never leave the exchange with zero rows / lose live positions. Only symbols
    absent from `state` are deleted, and that delete runs in the same
    transaction as the upserts.

    Retries up to 3 times with exponential-style backoff to survive transient
    DB failures that would otherwise leave ghost positions in trading_state.
    """
    if conn is None:
        return
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            with conn.cursor() as cur:
                for symbol, data in state.items():
                    cur.execute(
                        """INSERT INTO trading_state
                               (exchange, symbol, entry_price, entry_time, peak_plpc, quantity,
                                dca_level, signal_price, total_position_eur, tp_level, tp_sold_qty, updated_at)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                           ON CONFLICT (exchange, symbol) DO UPDATE SET
                               entry_price = EXCLUDED.entry_price,
                               entry_time  = EXCLUDED.entry_time,
                               peak_plpc   = EXCLUDED.peak_plpc,
                               quantity    = EXCLUDED.quantity,
                               dca_level   = EXCLUDED.dca_level,
                               signal_price = EXCLUDED.signal_price,
                               total_position_eur = EXCLUDED.total_position_eur,
                               tp_level    = EXCLUDED.tp_level,
                               tp_sold_qty = EXCLUDED.tp_sold_qty,
                               updated_at  = CURRENT_TIMESTAMP""",
                        (
                            exchange,
                            symbol,
                            data.get("entry_price"),
                            data.get("entry_time"),
                            data.get("peak_plpc", 0.0),
                            data.get("quantity", 0.0),
                            data.get("dca_level", 0),
                            data.get("signal_price"),
                            data.get("total_position_eur"),
                            data.get("tp_level", 0),
                            data.get("tp_sold_qty", 0.0),
                        ),
                    )
                # Prune positions that are no longer held (closed since last save).
                symbols = list(state.keys())
                if symbols:
                    cur.execute(
                        "DELETE FROM trading_state WHERE exchange = %s AND symbol <> ALL(%s)",
                        (exchange, symbols),
                    )
                else:
                    cur.execute("DELETE FROM trading_state WHERE exchange = %s", (exchange,))
            conn.commit()
            return
        except Exception as e:
            if attempt < max_attempts - 1:
                # Rollback so the connection is usable for the next attempt.
                try:
                    conn.rollback()
                except Exception:
                    pass
                time.sleep(0.5 * (attempt + 1))
            else:
                print(f"save_trading_state failed after {max_attempts} attempts: {e}", file=sys.stderr)
                try:
                    conn.rollback()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Notify State (αντικαθιστά last_notify.json / kraken_last_notify.json)
# ---------------------------------------------------------------------------

def load_notify_state(conn, exchange):
    """Load notify state for an exchange as a dict {last_notify_time: str, ...extra}."""
    if conn is None:
        return {"last_notify_time": "1970-01-01T00:00:00Z"}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT last_notify_time, extra FROM notify_state WHERE exchange = %s",
                (exchange,),
            )
            row = cur.fetchone()
        if row is None:
            return {"last_notify_time": "1970-01-01T00:00:00Z"}
        notify_time = row[0]
        extra = row[1] or {}
        result = {"last_notify_time": notify_time.isoformat().replace("+00:00", "Z") if notify_time else "1970-01-01T00:00:00Z"}
        result.update(extra)
        return result
    except Exception as e:
        if DEBUG:
            raise
        print(f"load_notify_state failed: {e}", file=sys.stderr)
        return {"last_notify_time": "1970-01-01T00:00:00Z"}


def save_notify_state(conn, exchange, state):
    """Upsert notify state for an exchange. state = {last_notify_time: str, ...extra}."""
    if conn is None:
        return
    try:
        notify_time = state.get("last_notify_time", "1970-01-01T00:00:00Z")
        # Extract extra keys (everything except last_notify_time)
        extra = {k: v for k, v in state.items() if k != "last_notify_time"}
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO notify_state (exchange, last_notify_time, extra, updated_at)
                   VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                   ON CONFLICT (exchange) DO UPDATE SET
                       last_notify_time = EXCLUDED.last_notify_time,
                       extra = EXCLUDED.extra,
                       updated_at = CURRENT_TIMESTAMP""",
                (exchange, notify_time, psycopg2.extras.Json(extra)),
            )
        conn.commit()
    except Exception as e:
        if DEBUG:
            raise
        print(f"save_notify_state failed: {e}", file=sys.stderr)
        try:
            conn.rollback()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Trade Log (αντικαθιστά .jsonl αρχεία)
# ---------------------------------------------------------------------------

def log_trade(conn, exchange, **kwargs):
    """Insert a trade log entry into trade_log table.

    kwargs: action, ticker, signal_strength, momentum_pct, entry_price,
            current_price, unrealized_plpc, order_id, client_order_id,
            quantity, estimated_value, position_size_pct, portfolio_equity, reason
    """
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO trade_log
                   (exchange, action, ticker, signal_strength, momentum_pct,
                    entry_price, current_price, unrealized_plpc, order_id,
                    client_order_id, quantity, estimated_value,
                    position_size_pct, portfolio_equity, reason,
                    regime, atr_at_entry, kelly_fraction, strategy_name)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                           %s, %s, %s, %s)""",
                (
                    exchange,
                    kwargs.get("action"),
                    kwargs.get("ticker"),
                    kwargs.get("signal_strength"),
                    kwargs.get("momentum_pct"),
                    kwargs.get("entry_price"),
                    kwargs.get("current_price"),
                    kwargs.get("unrealized_plpc"),
                    kwargs.get("order_id"),
                    kwargs.get("client_order_id"),
                    kwargs.get("quantity"),
                    kwargs.get("estimated_value"),
                    kwargs.get("position_size_pct"),
                    kwargs.get("portfolio_equity"),
                    kwargs.get("reason"),
                    kwargs.get("regime"),
                    kwargs.get("atr_at_entry"),
                    kwargs.get("kelly_fraction"),
                    kwargs.get("strategy_name"),
                ),
            )
        conn.commit()
        return True
    except Exception as e:
        if DEBUG:
            raise
        print(f"log_trade failed: {e}", file=sys.stderr)
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def log_successful_sell_once(conn, exchange, *, ticker, entry_price, price_hint,
                             quantity, order, reason):
    """Persist one successful close using the strategy's original exchange key.

    The order id makes retries after a successful execution idempotent.  Price
    and quantity are taken from the fill when CCXT supplies them; DB P/L stays
    in its canonical decimal-fraction unit.
    """
    from traders.common.exchange import extract_fill
    if not isinstance(order, dict):
        return False
    order_id = order.get("id")
    fill_price, fill_qty = extract_fill(order, price_hint)
    fill_qty = fill_qty if fill_qty is not None else quantity
    try:
        if order_id:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM trade_log WHERE exchange=%s AND action='SELL' AND order_id=%s LIMIT 1",
                            (exchange, order_id))
                if cur.fetchone():
                    return True
        plpc = ((fill_price - entry_price) / entry_price) if entry_price else 0.0
        return bool(log_trade(conn, exchange, action="SELL", ticker=ticker,
                              entry_price=entry_price, current_price=fill_price,
                              unrealized_plpc=plpc, order_id=order_id, quantity=fill_qty,
                              estimated_value=fill_price * fill_qty, reason=reason,
                              strategy_name=exchange))
    except Exception as e:
        _safe_rollback(conn)
        print(f"log_successful_sell_once failed: {e}", file=sys.stderr)
        return False


def coins_held_by_other_bots(conn, my_exchange):
    """Return set of base-symbols owned by OTHER kraken bots (not us).

    Prevents reconciliation from creating ghost duplicate positions when
    the AI Overseer (exchange='kraken') already owns a coin that the
    pullback/momentum bot sees in the shared wallet.

    Args:
        conn: psycopg2 connection
        my_exchange: this bot's exchange name (e.g. 'kraken-pullback')

    Returns:
        set of uppercase base symbols (e.g. {'AVAX', 'NEAR'})
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT DISTINCT symbol FROM trading_state
                   WHERE exchange LIKE %s
                     AND exchange <> %s
                     AND quantity > 0""",
                ('kraken%', my_exchange))
            return {base_symbol(r[0]) for r in cur.fetchall()}
    except Exception:
        return set()
