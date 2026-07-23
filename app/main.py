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
from app.render import partial  # noqa: E402
from app.settings import get_ai_config, set_setting  # noqa: E402

app = FastAPI(title="AITrader")
templates = Jinja2Templates("app/templates")


@app.on_event("startup")
async def _startup():
    init_schema()


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    from app.db import get_conn
    today_trades = []
    open_positions = []
    recent_reviews = []
    summary = {"total_trades": 0, "open_positions": 0, "today_trades": 0, "total_pl": 0.0}
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
            # Today's trades full list
            cur.execute("SELECT timestamp, exchange, action, ticker, entry_price, unrealized_plpc, quantity, reason FROM trade_log WHERE timestamp >= CURRENT_DATE ORDER BY timestamp DESC")
            today_trades = [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]
            # Open positions
            cur.execute("SELECT exchange, symbol, entry_price, entry_time, peak_plpc, quantity FROM trading_state ORDER BY entry_time DESC")
            open_positions = [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]
            # Current P/L from latest kraken price (Alpaca has no asset_prices data)
            for p in open_positions:
                p["current_pl"] = None
                if p["exchange"] == "kraken" and p["entry_price"]:
                    cur.execute(
                        "SELECT price FROM asset_prices WHERE exchange='kraken' AND symbol=%s ORDER BY timestamp DESC LIMIT 1",
                        (p["symbol"],))
                    row = cur.fetchone()
                    if row and row[0]:
                        p["current_pl"] = round((float(row[0]) - float(p["entry_price"])) / float(p["entry_price"]) * 100, 2)
            # Recent LLM reviews
            cur.execute("SELECT created_at, strategy, symbol, verdict, score, reason FROM llm_review_log ORDER BY created_at DESC LIMIT 10")
            recent_reviews = [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]
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

    ctx = {
        "script_count": script_count,
        "modes": modes,
        "last_orch_run": last_orch_run,
        "summary": summary,
        "today_trades": today_trades,
        "open_positions": open_positions,
        "recent_reviews": recent_reviews,
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


@app.get("/ui/admin/ai", response_class=HTMLResponse)
async def ai_config_form(request: Request):
    cfg = get_ai_config()
    ctx = {
        "model": cfg["model"] or "",
        "base_url": cfg["base_url"] or "",
        "api_key_masked": _mask_secret(cfg.get("api_key")),
        "has_api_key": bool(cfg.get("api_key")),
    }
    return templates.TemplateResponse(request, "admin_ai.html", ctx)


@app.post("/ui/admin/ai")
async def ai_config_save(request: Request):
    form = await request.form()
    model = (form.get("model") or "").strip()
    base_url = (form.get("base_url") or "").strip()
    api_key = (form.get("api_key") or "").strip()

    errors = []
    if not model:
        errors.append("Model is required.")
    if not base_url.startswith(("http://", "https://")):
        errors.append("Base URL must start with http:// or https://")

    if errors:
        cfg = get_ai_config()
        inner = partial(request, "_admin_ai_form.html",
                        flash=" ".join(errors), flash_type="err",
                        model=model or cfg.get("model") or "",
                        base_url=base_url or cfg.get("base_url") or "",
                        api_key_masked=_mask_secret(cfg.get("api_key")),
                        has_api_key=bool(cfg.get("api_key")))
        return HTMLResponse(f'<div id="ai-config">{inner}</div>')

    set_setting("ai.model", model)
    set_setting("ai.base_url", base_url)
    if api_key:
        set_setting("ai.api_key", api_key)

    cfg = get_ai_config()
    inner = partial(request, "_admin_ai_form.html",
                    flash="Settings saved.", flash_type="ok",
                    model=cfg["model"] or "",
                    base_url=cfg["base_url"] or "",
                    api_key_masked=_mask_secret(cfg.get("api_key")),
                    has_api_key=bool(cfg.get("api_key")))
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


@app.get("/ui/admin/cron/db-table", response_class=HTMLResponse)
async def cron_db_table(request: Request):
    from app.cron_orchestrator import list_jobs
    from app.db import get_conn
    with get_conn() as conn:
        jobs = list_jobs(conn)
    html = partial(request, "_admin_cron_db_table.html", jobs=jobs)
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
    html = partial(request, "_admin_cron_db_table.html", jobs=jobs)
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

    ctx = {
        "table": table,
        "columns": columns,
        "rows": rows,
        "filters": filters,
        "filter_qs": urlencode(filters),
        "sort": sort,
        "dir": dir,
        "page": page,
        "total_pages": total_pages,
        "total": total,
    }
    return templates.TemplateResponse(request, "data_table.html", ctx)
