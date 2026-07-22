"""Generic paginated/filterable table browsing for the admin Data UI."""

from __future__ import annotations

from psycopg2 import sql

from app.db import get_conn

TABLES = [
    "asset_prices",
    "cron_jobs",
    "cron_runs",
    "llm_review_log",
    "trade_log",
    "trading_state",
]

PAGE_SIZE = 20


def _columns(cur, table: str) -> list[str]:
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = %s ORDER BY ordinal_position",
        (table,),
    )
    return [r[0] for r in cur.fetchall()]


def query_table(table: str, filters: dict, sort: str, direction: str, page: int):
    """Return (columns, rows, total_rows, total_pages) for a whitelisted table."""
    if table not in TABLES:
        raise ValueError(f"unknown table: {table}")

    with get_conn() as conn:
        cur = conn.cursor()
        columns = _columns(cur, table)

        active = [(col, val) for col, val in filters.items() if col in columns and val]
        params = [f"%{val}%" for _, val in active]
        where = sql.SQL("")
        if active:
            clauses = [sql.SQL("{} :: text ILIKE %s").format(sql.Identifier(col)) for col, _ in active]
            where = sql.SQL(" WHERE ") + sql.SQL(" AND ").join(clauses)

        cur.execute(
            sql.SQL("SELECT COUNT(*) FROM {} {}").format(sql.Identifier(table), where),
            params,
        )
        total = cur.fetchone()[0]
        total_pages = max(1, -(-total // PAGE_SIZE))
        page = min(max(page, 1), total_pages)

        order = sql.SQL("")
        if sort in columns:
            order = sql.SQL(" ORDER BY {} {}").format(
                sql.Identifier(sort), sql.SQL("DESC" if direction == "desc" else "ASC")
            )

        cur.execute(
            sql.SQL("SELECT * FROM {} {} {} LIMIT %s OFFSET %s").format(
                sql.Identifier(table), where, order
            ),
            params + [PAGE_SIZE, (page - 1) * PAGE_SIZE],
        )
        rows = cur.fetchall()

        return columns, rows, total, total_pages, page
