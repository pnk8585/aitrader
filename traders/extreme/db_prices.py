import os
import sys
import psycopg2
import psycopg2.extras
from psycopg2.extras import execute_values
from dotenv import load_dotenv

# Load environment variables
env_path = "PROJECT_ROOT/.env"
load_dotenv(dotenv_path=env_path)

EXCHANGE = "kraken"


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
    except Exception as e:
        print(f"DB connection failed: {e}", file=sys.stderr)
        return None

    try:
        ensure_schema(conn)
    except Exception as e:
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
            """
            CREATE UNIQUE INDEX IF NOT EXISTS trading_state_exchange_symbol_key
            ON trading_state (exchange, symbol)
            """
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
                reason TEXT
            )
            """
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
        print(f"close_connection failed: {e}", file=sys.stderr)


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
                "SELECT symbol, entry_price, entry_time, peak_plpc, quantity FROM trading_state WHERE exchange = %s",
                (exchange,),
            )
            rows = cur.fetchall()
        state = {}
        for symbol, ep, et, peak, qty in rows:
            state[symbol] = {
                "entry_price": float(ep) if ep else 0.0,
                "entry_time": et.isoformat().replace("+00:00", "Z") if et else None,
                "peak_plpc": float(peak) if peak else 0.0,
                "quantity": float(qty) if qty else 0.0,
            }
        return state
    except Exception as e:
        print(f"load_trading_state failed: {e}", file=sys.stderr)
        return {}


def save_trading_state(conn, exchange, state):
    """Persist all positions for an exchange via UPSERT, then prune rows no
    longer held. state = {symbol: {entry_price, entry_time, peak_plpc, quantity}}.

    Uses ON CONFLICT upsert (not DELETE-all + INSERT) so a crash mid-write can
    never leave the exchange with zero rows / lose live positions. Only symbols
    absent from `state` are deleted, and that delete runs in the same
    transaction as the upserts.
    """
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            for symbol, data in state.items():
                cur.execute(
                    """INSERT INTO trading_state
                           (exchange, symbol, entry_price, entry_time, peak_plpc, quantity, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                       ON CONFLICT (exchange, symbol) DO UPDATE SET
                           entry_price = EXCLUDED.entry_price,
                           entry_time  = EXCLUDED.entry_time,
                           peak_plpc   = EXCLUDED.peak_plpc,
                           quantity    = EXCLUDED.quantity,
                           updated_at  = CURRENT_TIMESTAMP""",
                    (
                        exchange,
                        symbol,
                        data.get("entry_price"),
                        data.get("entry_time"),
                        data.get("peak_plpc", 0.0),
                        data.get("quantity", 0.0),
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
    except Exception as e:
        print(f"save_trading_state failed: {e}", file=sys.stderr)
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
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO trade_log
                   (exchange, action, ticker, signal_strength, momentum_pct,
                    entry_price, current_price, unrealized_plpc, order_id,
                    client_order_id, quantity, estimated_value,
                    position_size_pct, portfolio_equity, reason)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
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
                ),
            )
        conn.commit()
    except Exception as e:
        print(f"log_trade failed: {e}", file=sys.stderr)
        try:
            conn.rollback()
        except Exception:
            pass
