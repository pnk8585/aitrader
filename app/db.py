"""psycopg2 connection factory. No SQLAlchemy — raw SQL only."""

from __future__ import annotations

import os
from contextlib import contextmanager

import psycopg2
import psycopg2.extensions


def _conn_kwargs() -> dict:
    return {
        "host": os.environ["DB_HOST"],
        "port": int(os.environ["DB_PORT"]),
        "dbname": os.environ["DB_NAME"],
        "user": os.environ["DB_USER"],
        "password": os.environ["DB_PASSWORD"],
    }


@contextmanager
def get_conn():
    """Yield a psycopg2 connection. Commits on clean exit, rolls back on exception."""
    conn = psycopg2.connect(connect_timeout=3, **_conn_kwargs())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_schema():
    """Idempotent DDL — runs on app startup."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS app_settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TIMESTAMPTZ DEFAULT now()
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cron_jobs (
                    name             TEXT PRIMARY KEY,
                    schedule_seconds INTEGER NOT NULL,
                    mode             TEXT DEFAULT 'live',
                    enabled          BOOLEAN DEFAULT TRUE,
                    next_run_at      TIMESTAMPTZ,
                    updated_at       TIMESTAMPTZ DEFAULT now()
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cron_runs (
                    id          SERIAL PRIMARY KEY,
                    job_name    TEXT REFERENCES cron_jobs(name),
                    status      TEXT,
                    started_at  TIMESTAMPTZ,
                    finished_at TIMESTAMPTZ,
                    summary     TEXT,
                    duration_ms INTEGER
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS universe_symbols (
                    asset_class TEXT NOT NULL,
                    symbol      TEXT NOT NULL,
                    enabled     BOOLEAN DEFAULT TRUE,
                    created_at  TIMESTAMPTZ DEFAULT now(),
                    PRIMARY KEY (asset_class, symbol),
                    CONSTRAINT universe_asset_class_chk
                        CHECK (asset_class IN ('stock', 'crypto'))
                );
            """)
