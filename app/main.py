"""AITrader admin UI — FastAPI + HTMX + Jinja2."""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

load_dotenv()

from app.db import init_schema  # noqa: E402
from app.logging_setup import (  # noqa: E402
    apply_log_level,
    apply_log_level_from_settings,
    configure_logging,
)
from app.render import partial  # noqa: E402
from app.settings import get_ai_config, get_setting, set_setting  # noqa: E402

# Quiet access logs until DB level applied (default INFO hides them)
configure_logging()

app = FastAPI(title="AITrader")
templates = Jinja2Templates("app/templates")


@app.on_event("startup")
async def _startup():
    init_schema()
    try:
        from app.settings import seed_default_settings
        seed_default_settings()
    except Exception as e:
        print(f"[startup] seed_default_settings: {e}")
    try:
        apply_log_level_from_settings()
    except Exception as e:
        print(f"[startup] apply_log_level: {e}")


# ── Dashboard table queries (server-side search / date / sort) ───

# (display cols, search cols, date col) per dashboard table
_TRADES_COLS = ["timestamp", "exchange", "ticker", "entry_price", "unrealized_plpc", "quantity", "reason"]
_TRADES_SEARCH = ["exchange", "ticker", "action", "reason"]
_REVIEWS_COLS = ["created_at", "strategy", "symbol", "verdict", "score", "reason"]
_REVIEWS_SEARCH = ["strategy", "symbol", "verdict", "reason"]
_DASH_LIMIT = 5  # ponytail: show last 5 on dashboard


def _date_clause(date_col: str, date: str):
    from psycopg2 import sql
    c = sql.Identifier(date_col)
    if date == "today":
        return sql.SQL("{c} >= CURRENT_DATE").format(c=c)
    if date == "yesterday":
        return sql.SQL("{c} >= CURRENT_DATE - INTERVAL '1 day' AND {c} < CURRENT_DATE").format(c=c)
    if date == "month":
        return sql.SQL("date_trunc('month', {c}) = date_trunc('month', CURRENT_DATE)").format(c=c)
    if date == "year":
        return sql.SQL("date_trunc('year', {c}) = date_trunc('year', CURRENT_DATE)").format(c=c)
    return None


def _dash_rows(table, cols, search_cols, date_col, search, date, sort, direction, default_sort):
    from psycopg2 import sql

    from app.db import get_conn

    where, params = [], []
    if search:
        likes = [sql.SQL("{}::text ILIKE %s").format(sql.Identifier(c)) for c in search_cols]
        where.append(sql.SQL("(") + sql.SQL(" OR ").join(likes) + sql.SQL(")"))
        params += [f"%{search}%"] * len(search_cols)
    dc = _date_clause(date_col, date)
    if dc is not None:
        where.append(dc)
    where_sql = (sql.SQL(" WHERE ") + sql.SQL(" AND ").join(where)) if where else sql.SQL("")

    sort_col = sort if sort in cols else default_sort
    order = sql.SQL(" ORDER BY {} {}").format(
        sql.Identifier(sort_col), sql.SQL("ASC" if direction == "asc" else "DESC"))

    query = sql.SQL("SELECT {} FROM {}{}{} LIMIT %s").format(
        sql.SQL(", ").join(sql.Identifier(c) for c in cols),
        sql.Identifier(table), where_sql, order)
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(query, params + [_DASH_LIMIT])
            return [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
    except Exception:
        return []


@app.get("/ui/dashboard/trades", response_class=HTMLResponse)
async def dash_trades(request: Request, search: str = "", date: str = "all", sort: str = "timestamp", dir: str = "desc"):
    rows = _dash_rows("trade_log", _TRADES_COLS, _TRADES_SEARCH, "timestamp", search, date, sort, dir, "timestamp")
    return HTMLResponse(partial(request, "_dash_trades.html", rows=rows, sort=sort, dir=dir))


@app.get("/ui/dashboard/reviews", response_class=HTMLResponse)
async def dash_reviews(request: Request, search: str = "", date: str = "all", sort: str = "created_at", dir: str = "desc"):
    rows = _dash_rows("llm_review_log", _REVIEWS_COLS, _REVIEWS_SEARCH, "created_at", search, date, sort, dir, "created_at")
    return HTMLResponse(partial(request, "_dash_reviews.html", rows=rows, sort=sort, dir=dir))


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, tsearch: str = "", tdate: str = "all", rsearch: str = "", rdate: str = "all"):
    from app.db import get_conn
    from app.wallets import kraken_balance, alpaca_balance
    today_trades = _dash_rows("trade_log", _TRADES_COLS, _TRADES_SEARCH, "timestamp", tsearch, tdate, "timestamp", "desc", "timestamp")
    open_positions = []
    recent_reviews = _dash_rows("llm_review_log", _REVIEWS_COLS, _REVIEWS_SEARCH, "created_at", rsearch, rdate, "created_at", "desc", "created_at")
    summary = {"total_trades": 0, "open_positions": 0, "today_trades": 0, "total_pl": 0.0}
    kraken = kraken_balance()
    alpaca = alpaca_balance()
    script_count = 0
    modes = {}
    last_orch_run = "never"
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM trade_log")
            summary["total_trades"] = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM trading_state")
            summary["open_positions"] = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM trade_log WHERE timestamp >= CURRENT_DATE")
            summary["today_trades"] = cur.fetchone()[0]
            cur.execute("SELECT COALESCE(SUM(peak_plpc), 0) FROM trading_state")
            summary["total_pl"] = round(float(cur.fetchone()[0] or 0), 2)
            # Open positions — exclude ghost positions (quantity=0 or entry_price=0)
            cur.execute("SELECT exchange, symbol, entry_price, entry_time, peak_plpc, quantity FROM trading_state WHERE quantity > 0 AND entry_price > 0 ORDER BY entry_time DESC")
            open_positions = [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]
            # Enrich with current price, P/L, and position value
            for p in open_positions:
                p["current_price"] = None
                p["current_pl"] = None
                p["position_value"] = None
                if p["entry_price"]:
                    if p["exchange"] == "kraken":
                        # trading_state stores "AVAX/EUR", asset_prices stores "AVAX" — strip quote currency
                        sym = p["symbol"].split("/")[0] if "/" in str(p["symbol"]) else p["symbol"]
                        cur.execute(
                            "SELECT price FROM asset_prices WHERE exchange='kraken' AND symbol=%s ORDER BY timestamp DESC LIMIT 1",
                            (sym,))
                        row = cur.fetchone()
                        if row and row[0]:
                            cp = float(row[0])
                            p["current_price"] = round(cp, 6)
                            p["current_pl"] = round((cp - float(p["entry_price"])) / float(p["entry_price"]) * 100, 2)
                            qty = float(p.get("quantity", 0) or 0)
                            p["position_value"] = round(cp * qty, 2)
            # Cron jobs stats
            cur.execute("SELECT COUNT(*) FROM cron_jobs WHERE enabled = TRUE")
            script_count = cur.fetchone()[0]
            cur.execute("SELECT mode, COUNT(*) FROM cron_jobs WHERE enabled = TRUE GROUP BY mode")
            for mode, cnt in cur.fetchall():
                modes[mode] = cnt
            cur.execute("SELECT MAX(updated_at) FROM cron_jobs")
            row = cur.fetchone()
            if row and row[0]:
                last_orch_run = row[0].strftime("%d/%m %H:%M")
    except Exception:
        pass

    # Compute truly free cash (subtract open position values from wallet)
    kraken_positions_value = sum(p.get("position_value", 0) or 0 for p in open_positions if p.get("exchange") == "kraken")
    kraken["positions_value"] = round(kraken_positions_value, 2)
    kraken["truly_free"] = round(kraken.get("free_eur", 0) - kraken_positions_value, 2) if kraken.get("free_eur") is not None else None

    ctx = {
        "script_count": script_count,
        "modes": modes,
        "last_orch_run": last_orch_run,
        "summary": summary,
        "today_trades": today_trades,
        "open_positions": open_positions,
        "recent_reviews": recent_reviews,
        "tsearch": tsearch,
        "tdate": tdate,
        "rsearch": rsearch,
        "rdate": rdate,
        "kraken": kraken,
        "alpaca": alpaca,
    }
    return templates.TemplateResponse(request, "dashboard.html", ctx)


@app.get("/ui/admin", response_class=HTMLResponse)
async def admin(request: Request):
    return templates.TemplateResponse(request, "admin.html", {})


# ── AI Config ────────────────────────────────────────────────

def _mask_secret(value: str | None) -> str:
    if not value:
        return ""
    return "••••" + value[-4:]


def _ai_form_ctx(**overrides):
    cfg = get_ai_config()
    log_level = (get_setting("logging.level") or "INFO").upper()
    if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        log_level = "INFO"
    ctx = {
        "model": cfg["model"] or "",
        "base_url": cfg["base_url"] or "",
        "api_key_masked": _mask_secret(cfg.get("api_key")),
        "has_api_key": bool(cfg.get("api_key")),
        "logging_level": log_level,
    }
    ctx.update(overrides)
    return ctx


@app.get("/ui/admin/ai", response_class=HTMLResponse)
async def ai_config_form(request: Request):
    return templates.TemplateResponse(request, "admin_ai.html", _ai_form_ctx())


@app.post("/ui/admin/ai")
async def ai_config_save(request: Request):
    form = await request.form()
    model = (form.get("model") or "").strip()
    base_url = (form.get("base_url") or "").strip()
    api_key = (form.get("api_key") or "").strip()
    log_level = (form.get("logging_level") or "").strip().upper()

    errors = []
    if not model:
        errors.append("Model is required.")
    if not base_url.startswith(("http://", "https://")):
        errors.append("Base URL must start with http:// or https://")
    if log_level and log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        errors.append("Invalid log level.")

    if errors:
        inner = partial(request, "_admin_ai_form.html",
                        flash=" ".join(errors), flash_type="err",
                        **_ai_form_ctx(
                            model=model or get_ai_config().get("model") or "",
                            base_url=base_url or get_ai_config().get("base_url") or "",
                            logging_level=log_level or "INFO",
                        ))
        return HTMLResponse(f'<div id="ai-config">{inner}</div>')

    set_setting("ai.model", model)
    set_setting("ai.base_url", base_url)
    if api_key:
        set_setting("ai.api_key", api_key)
    if log_level in ("DEBUG", "INFO", "WARNING", "ERROR"):
        set_setting("logging.level", log_level)
        apply_log_level(log_level)

    inner = partial(request, "_admin_ai_form.html",
                    flash="Settings saved.", flash_type="ok",
                    **_ai_form_ctx())
    return HTMLResponse(f'<div id="ai-config">{inner}</div>')


# ── Telegram ──────────────────────────────────────────────


@app.get("/ui/admin/telegram", response_class=HTMLResponse)
async def telegram_form(request: Request):
    from app.settings import get_setting
    token = get_setting("telegram.token") or ""
    ctx = {
        "masked_token": _mask_secret(token),
        "token_set": bool(token),
        "chat_id": get_setting("telegram.chat_id") or "",
        "enabled": get_setting("telegram.enabled") == "true",
    }
    return templates.TemplateResponse(request, "admin_telegram.html", ctx)


@app.post("/ui/admin/telegram")
async def telegram_save(request: Request):
    from app.settings import set_setting
    data = await request.form()
    token = (data.get("token") or "").strip()
    chat_id = (data.get("chat_id") or "").strip()
    enabled = data.get("enabled") == "true"
    errors = []
    if chat_id and not chat_id.isdigit():
        errors.append("Chat ID must be numeric")
    if errors:
        from app.render import partial
        err_html = "".join(f'<div class="flash flash-err">{e}</div>' for e in errors)
        return HTMLResponse(err_html)
    if token:
        set_setting("telegram.token", token)
    set_setting("telegram.chat_id", chat_id)
    set_setting("telegram.enabled", "true" if enabled else "false")
    from app.render import partial
    flash = '<div class="flash flash-ok">Saved</div>'
    return HTMLResponse(flash)


@app.post("/ui/admin/telegram/test")
async def telegram_test(request: Request):
    from app.notify import send_test
    result = send_test()
    if result["ok"]:
        return HTMLResponse('<div class="flash flash-ok">Test message sent</div>')
    return HTMLResponse(f'<div class="flash flash-err">{result["error"]}</div>')


# ── Cron ───────────────────────────────────────────────────

@app.get("/ui/admin/cron", response_class=HTMLResponse)
async def cron_page(request: Request):
    from app.cron_orchestrator import list_jobs
    from app.db import get_conn
    with get_conn() as conn:
        jobs = list_jobs(conn)
    return templates.TemplateResponse(request, "admin_cron.html", {"jobs": jobs})


_CRON_SORT_COLS = {"name", "mode", "schedule_seconds", "enabled", "next_run_at", "updated_at"}


@app.get("/ui/admin/cron/db-table", response_class=HTMLResponse)
async def cron_db_table(request: Request, sort: str = "", dir: str = "asc"):
    from app.cron_orchestrator import list_jobs
    from app.db import get_conn
    with get_conn() as conn:
        jobs = list_jobs(conn)
    if sort in _CRON_SORT_COLS:
        jobs.sort(key=lambda j: (j[sort] is None, j[sort]), reverse=(dir == "desc"))
    html = partial(request, "_admin_cron_db_table.html", jobs=jobs, sort=sort, dir=dir)
    return HTMLResponse(html)


@app.post("/ui/admin/cron-jobs/{name}/run", response_class=HTMLResponse)
async def cron_job_run(name: str, request: Request):
    from app.cron_orchestrator import list_jobs, run_job
    from app.db import get_conn
    with get_conn() as conn:
        try:
            run_job(conn, name)
        except Exception as e:
            return HTMLResponse(
                f'<div class="flash flash-err">{e}</div>', status_code=400
            )
        jobs = list_jobs(conn)
    html = partial(request, "_admin_cron_db_table.html", jobs=jobs, sort="", dir="asc")
    return HTMLResponse(html)


@app.post("/ui/admin/positions/{exchange}/{symbol}/sell", response_class=HTMLResponse)
async def position_sell(exchange: str, symbol: str, request: Request):
    from app.db import get_conn
    from app.notify import send_telegram
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM trading_state WHERE exchange=%s AND symbol=%s",
            (exchange, symbol))
        if cur.fetchone() is None:
            return HTMLResponse(
                '<div class="flash flash-err">Position not found</div>', status_code=404)
    send_telegram(f"🔴 SELL REQUEST: {exchange} {symbol} @ market")
    return HTMLResponse(
        f'<div class="flash">Sell request sent for {exchange} {symbol}</div>')


@app.get("/ui/admin/cron-jobs")
async def cron_jobs_api(request: Request):
    from app.cron_orchestrator import list_jobs
    from app.db import get_conn
    with get_conn() as conn:
        return list_jobs(conn)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# ── Data browser ─────────────────────────────────────────────

@app.get("/ui/admin/data", response_class=HTMLResponse)
async def data_index(request: Request):
    from app.data import TABLES
    return templates.TemplateResponse(request, "data_index.html", {"tables": TABLES})


@app.get("/ui/admin/data/{table}", response_class=HTMLResponse)
async def data_table_view(table: str, request: Request, page: int = 1, sort: str = "", dir: str = "asc"):
    from urllib.parse import urlencode

    from app.data import TABLES, query_table

    if table not in TABLES:
        return HTMLResponse("Not found", status_code=404)

    reserved = {"page", "sort", "dir"}
    filters = {k: v for k, v in request.query_params.items() if k not in reserved and v}
    columns, rows, total, total_pages, page = query_table(table, filters, sort, dir, page)

    # Find first date-like column for client-side date filter
    date_col = -1
    for i, col in enumerate(columns):
        if "timestamp" in col or "date" in col or col.endswith("_at"):
            date_col = i
            break

    ctx = {
        "table": table,
        "columns": columns,
        "rows": rows,
        "filters": filters,
        "filter_qs": urlencode(filters),
        "sort": sort,
        "dir": dir,
        "date_col": date_col,
        "page": page,
        "total_pages": total_pages,
        "total": total,
    }
    return templates.TemplateResponse(request, "data_table.html", ctx)
