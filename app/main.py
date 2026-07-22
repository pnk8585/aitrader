"""AITrader admin UI — FastAPI + HTMX + Jinja2."""

from __future__ import annotations

import json
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

load_dotenv()

from app import cron  # noqa: E402
from app.db import init_schema  # noqa: E402
from app.render import partial  # noqa: E402
from app.settings import get_ai_config, set_setting  # noqa: E402
from app.state import _STATE_DIR  # noqa: E402

app = FastAPI(title="AITrader")
templates = Jinja2Templates("app/templates")


@app.on_event("startup")
async def _startup():
    init_schema()


def _read_registry() -> dict:
    """Read registry JSON, trying both possible paths."""
    for name in ("registry.json", "aitrader_orchestrator.json"):
        path = _STATE_DIR / name
        if path.exists():
            return json.loads(path.read_text())
    return {}


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    reg = _read_registry()
    scripts = reg.get("scripts", {})
    orchestrator = reg.get("orchestrator", {})
    modes = {}
    for s in scripts.values():
        m = s.get("mode", "unknown")
        modes[m] = modes.get(m, 0) + 1

    # Recent trade data
    from app.db import get_conn
    recent_trades = []
    recent_reviews = []
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT timestamp, exchange, action, ticker, entry_price, unrealized_plpc FROM trade_log ORDER BY timestamp DESC LIMIT 5")
            recent_trades = [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]
            cur.execute("SELECT created_at, strategy, symbol, verdict, score, reason FROM llm_review_log ORDER BY created_at DESC LIMIT 5")
            recent_reviews = [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]
    except Exception:
        pass

    ctx = {
        "script_count": len(scripts),
        "modes": modes,
        "last_orch_run": orchestrator.get("last_run", "never"),
        "scripts": scripts,
        "recent_trades": recent_trades,
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
    scripts = cron.list_scripts()
    return templates.TemplateResponse(request, "admin_cron.html", {"scripts": scripts})


@app.get("/ui/admin/cron/table", response_class=HTMLResponse)
async def cron_table(request: Request):
    scripts = cron.list_scripts()
    html = partial(request, "_admin_cron_table.html", scripts=scripts)
    return HTMLResponse(html)


@app.post("/ui/admin/cron/{name}/mode", response_class=HTMLResponse)
async def cron_set_mode(name: str, request: Request):
    data = await request.form()
    mode = data.get("mode", "")
    try:
        cron.set_mode(name, mode)
    except (ValueError, KeyError) as e:
        return HTMLResponse(
            f'<div class="flash flash-err">{e}</div>', status_code=400
        )
    scripts = cron.list_scripts()
    html = partial(request, "_admin_cron_table.html", scripts=scripts)
    return HTMLResponse(html)


@app.post("/ui/admin/cron/{name}/run", response_class=HTMLResponse)
async def cron_run_now(name: str, request: Request):
    try:
        cron.run_now(name)
    except KeyError as e:
        return HTMLResponse(
            f'<div class="flash flash-err">{e}</div>', status_code=400
        )
    scripts = cron.list_scripts()
    html = partial(request, "_admin_cron_table.html", scripts=scripts)
    return HTMLResponse(html)


@app.post("/ui/admin/cron/{name}/pause", response_class=HTMLResponse)
async def cron_pause(name: str, request: Request):
    try:
        cron.pause(name)
    except KeyError as e:
        return HTMLResponse(
            f'<div class="flash flash-err">{e}</div>', status_code=400
        )
    scripts = cron.list_scripts()
    html = partial(request, "_admin_cron_table.html", scripts=scripts)
    return HTMLResponse(html)


@app.get("/ui/admin/cron-jobs")
async def cron_jobs_api(request: Request):
    from app.cron_orchestrator import list_jobs
    from app.db import get_conn
    with get_conn() as conn:
        return list_jobs(conn)


@app.post("/ui/admin/cron/{name}/resume", response_class=HTMLResponse)
async def cron_resume(name: str, request: Request):
    try:
        cron.resume(name)
    except KeyError as e:
        return HTMLResponse(
            f'<div class="flash flash-err">{e}</div>', status_code=400
        )
    scripts = cron.list_scripts()
    html = partial(request, "_admin_cron_table.html", scripts=scripts)
    return HTMLResponse(html)


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
