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
│       ├─ alpaca-stocks (LIVE, 5m)
│       ├─ position-monitor (LIVE, 2h)
│       ├─ end-of-day-review (LIVE, 24h)
│       └─ db-cleanup (LIVE, 24h)
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
| Logging | `app/logging_setup.py` | stdout + `/state/logs` |
| LLM review | `traders/common/llm_review.py` | Sync trade evaluation |
| Exchange | `traders/common/exchange.py` | Paper/live order routing |
| Gates | `traders/common/gates.py` | Safety pause (BTC drawdown, manual halt) |
| DB prices | `traders/extreme/db_prices.py` | asset_prices queries |
| Position monitor | `position_monitor.py` | LLM exits |
| Pullback | `traders/crypto_trades/kraken_pullback.py` | Entries |
| Momentum | `traders/crypto_trades/kraken_momentum.py` | Entries |
| Alpaca | `traders/trades/alpaca_stocks.py` | Stock momentum |

## Guidelines
- **Write**: Save new info to the correct file — same one you found it in.
- **Secrets/keys**: Never in project files — Bitwarden only.
- **Job config**: DB `cron_jobs` / admin UI — not JSON orchestrator files.
- **LLM model**: `hermes-flash` via LiteLLM (`host.docker.internal:4000` from container).
- **Container**: Admin UI **and** scheduler inside Docker. Deploy via CI/CD only.
- **Trade notifications**: Only BUY/SELL events → Telegram. No HOLD/SKIP spam.
- **CI**: `dockerhub.pkatopodis.me` — image + homeserver compose pull/up.

## Docker

```bash
bash scripts/init_state_dir.sh
# .env: DB_HOST=host.docker.internal
# Production: ~/homeserver/docker-compose/aitrader/docker-compose.yml
# Deploy: GitLab CI (do not manual-deploy from agents)
```

**State volume** `/home/pank/docker-data/aitrader` → `/state`:
- logs: `scheduler.log`, `cron.log`, `jobs/<name>.log`, locks
- `.env`, gates under `ai_overseer/`
