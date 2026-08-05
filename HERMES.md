# HERMES.md — Project Entry Point

## Overview
Multi-strategy automated trading system (AITrader) for Kraken crypto and Alpaca US stocks.

## Architecture

```
Docker: start.py
├─ scheduler.py (tick every 60s)
│    └─ app/cron_orchestrator.py → JOB_REGISTRY / cron_jobs
│       ├─ kraken-pullback (LIVE, 5m)
│       ├─ kraken-momentum (PAPER, 5m)
│       ├─ kraken-high-risk (PAPER, 5m)
│       ├─ alpaca-stocks (LIVE, 5m)
│       ├─ position-monitor (LIVE, 2h)
│       ├─ end-of-day-review (LIVE, 24h)
│       ├─ health-check (LIVE, 24h, 21:00 Athens)
│       ├─ hourly-report (LIVE, 3600s — silent unless trade signals)
│       ├─ weekly-rethink (LIVE, Sunday 09:00 Athens)
│       └─ db-cleanup (LIVE, ~05:00 Athens — prices downsample + cron_runs 24h)
└─ uvicorn app.main:app :9237  (admin UI)
```

**LLM evaluation** is synchronous — every buy/sell decision goes through `traders/common/llm_review.py` which:
- Queries DB for price context (1h/6h/24h, BTC, 24h range)
- Fetches recent news headlines (DuckDuckGo, 4s timeout)
- Calls the configured model via LiteLLM proxy
- Logs every verdict to `llm_review_log` for later accuracy analysis

**Modes:** `live` | `paper` | `paused` (per job in `cron_jobs`)

## Project Info

| Topic | File | Purpose |
|-------|------|---------|
| Entry | `start.py` | Scheduler + uvicorn in one process |
| Scheduler | `scheduler.py` | 60s tick loop |
| Jobs | `app/cron_orchestrator.py` | JOB_REGISTRY, run, Telegram notify |
| Logging | `app/logging_setup.py` | stdout + `/state/logs`; `logging.level` in DB (default INFO); access→DEBUG |
| LLM review | `traders/common/llm_review.py` | Sync trade evaluation |
| Exchange | `traders/common/exchange.py` | Paper/live order routing |
| Gates | `traders/common/gates.py` | Safety pause (BTC drawdown, manual halt) |
| DB prices | `traders/extreme/db_prices.py` | asset_prices queries |
| Position monitor | `position_monitor.py` | LLM exits |
| Weekly rethink | `traders/weekly_rethink.py` | Sunday strategy mining (read-only) |
| Pullback | `traders/crypto_trades/kraken_pullback.py` | Entries |
| Momentum | `traders/crypto_trades/kraken_momentum.py` | Entries |
| High Risk | `traders/crypto_trades/kraken_high_risk.py` | Entries |
| Alpaca | `traders/trades/alpaca_stocks.py` | Stock momentum |

## Guidelines
- **Write**: Save new info to the correct file — same one you found it in.
- **Secrets/keys**: Never in project files — Bitwarden only.
- **Job config**: DB `cron_jobs` / admin UI — not JSON orchestrator files.
- **Job mode (live/paper) = DB-driven**: `SELECT name, mode FROM cron_jobs` — πάντα check από το container (`docker exec aitrader python -m app.cron_orchestrator list`), ΟΧΙ registry defaults/docs. Τα defaults στον κώδικα (π.χ. momentum/grid paper) ΔΕΝ ισχύουν αν το DB λέει αλλιώς — το DB υπερισχύει.
- **LLM model**: `hermes-flash` via LiteLLM (`host.docker.internal:4000` from container).
- **Container**: Admin UI **and** scheduler inside Docker. Deploy via CI/CD only.
- **Trade notifications**: Only BUY/SELL events → Telegram. No HOLD/SKIP spam.
- **CI**: `dockerhub.pkatopodis.me` — image + homeserver compose pull/up.
- **DB driver**: `psycopg2` must use `autocommit=True`. Without it, `InFailedSqlTransaction` cascades after the first error — `except` + `rollback()` alone is NOT enough.
- **HTMX in admin UI**: Serve locally — CDN silently fails inside Docker (network policy), pages render blank with no console error.
- **Paper ↔ live toggle**: Dashboard cron flips `cron_jobs.mode` via HTMX partial (`_paper_live.html`); UI polls at DEBUG only.
- **Phases**: P1–P5 complete (cron registry, multi-strategy, gates, dashboard, paper/live switch). No open phase items.

## Docker

```bash
bash scripts/init_state_dir.sh
# .env: DB_HOST=host.docker.internal
# Production: ~/homeserver/docker-compose/aitrader/docker-compose.yml
# Deploy: GitLab CI (do not manual-deploy from agents)
```

### Deploy flow (gitlab remote)

- **Push στο `gitlab` remote** (`gitlab.pkatopodis.me/aiagents/aitrader`) — είναι το
  πραγματικό CI/CD. Το `origin` (GitHub) είναι mirror **χωρίς CI/CD** — μην κάνεις
  deploy από εκεί (check πάντα `git remote -v`).
- `git push gitlab main` → pipeline ~5 λεπτά → deploy στο homeserver.
- **Verify**: `docker inspect aitrader --format '{{.Created}}'` (νέο timestamp) +
  startup log `"llm_prompts: seeded/upgraded N defaults"`.
- `llm_prompts` source of truth = `DEFAULT_PROMPTS` in `app/llm_prompts.py`.
  Startup `seed_prompts()` inserts missing keys and upgrades known legacy weak
  defaults (JSON-only one-liners) → DeepSeek-style OUTPUT RULES. Does **not**
  overwrite admin customizations. Force full reset: `resync_prompts()`.
- `GITLAB_TOKEN` διαθέσιμο στο hermes env.

**State volume** `/home/pank/docker-data/aitrader` → `/state`:
- logs: `scheduler.log`, `cron.log`, `jobs/<name>.log`, `llm.jsonl` (structured LLM audit), locks
- `.env`, gates under `ai_overseer/`
- Disable LLM file audit: `LOG_LLM_PROMPTS=0` (DB `llm_review_log` still used)
- Log level: `app_settings.logging.level` (INFO default) or Admin → AI; access polls only at DEBUG
