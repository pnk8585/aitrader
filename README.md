# AITrader

Multi-strategy automated trading system — **Kraken crypto** + **Alpaca US stocks**, with LLM-evaluated entries and exits.

Runs as a **Docker container**: FastAPI admin UI + in-process scheduler (tick every 60s).  
CI/CD deploys the image; production compose is `~/homeserver/docker-compose/aitrader/`.

## Architecture

```
start.py → scheduler.py (every 60s) + uvicorn :9237
              │
              └─ app/cron_orchestrator.py  (JOB_REGISTRY / DB cron_jobs)
                 ├─ kraken-pullback     LIVE   5m
                 ├─ kraken-momentum     PAPER  5m
                 ├─ alpaca-stocks       PAPER  5m
                 ├─ position-monitor    LIVE   2h
                 ├─ end-of-day-review   LIVE   24h
                 └─ db-cleanup          LIVE   24h
```

Every buy/sell is evaluated by a local LLM (`traders/common/llm_review.py`) that gets:
- **Price context**: 1h/6h/24h change, BTC price, 24h range (from `asset_prices` DB)
- **News headlines**: 3 latest headlines from DuckDuckGo (4s timeout)
- **Signal scores**: strategy-specific scoring from candidate analysis

All decisions logged to `llm_review_log` for retrospective accuracy analysis.

## Quick start

```bash
# One-time state dir
bash scripts/init_state_dir.sh
# Edit /home/pank/docker-data/aitrader/.env — DB_HOST=host.docker.internal

# Local run (dev)
docker compose up -d --build
# UI: http://localhost:9237  ·  Health: curl http://localhost:9237/healthz

# Manual job tick (from host venv, against same DB)
python -m scripts.cron_runner --mode tick
```

## Script modes

| Mode | Orders | DB logging |
|------|--------|-----------|
| `live` | Real exchange API | Real trades |
| `paper` | Simulated fills | `paper-` prefix |
| `paused` | Skipped entirely | — |

Modes live in the `cron_jobs` table (admin UI / DB). Env `AITRADER_MODE` is set per run by the orchestrator.

## Database tables

| Table | Purpose |
|-------|---------|
| `trade_log` | Every executed trade |
| `llm_review_log` | Every LLM evaluation (APPROVE/REJECT/SELL/HOLD) |
| `trading_state` | Open position tracking |
| `asset_prices` | 5-min price snapshots (Kraken) |
| `cron_jobs` / `cron_runs` | Scheduler registry + run history |

## Project layout

```
aitrader/
├── start.py / scheduler.py      # Container entry + 60s tick loop
├── position_monitor.py          # Exit monitor job
├── app/                         # FastAPI UI, DB, cron_orchestrator, logging
├── scripts/
│   ├── cron_runner.py           # Manual tick / run-jobs
│   ├── db_cleanup.py            # Daily cleanup job
│   ├── init_state_dir.sh        # Bootstrap /state volume
│   └── pnl_dashboard.py         # Manual P&L report
├── traders/
│   ├── common/                  # llm_review, exchange, gates, config
│   ├── crypto_trades/           # kraken pullback + momentum
│   ├── trades/alpaca_stocks.py
│   ├── strategies/              # pullback + momentum modules
│   ├── eod_review.py
│   └── extreme/db_prices.py     # Shared price/DB helpers
├── research/backtest_pullback.py
└── tests/
```

## Logs

With `AITRADER_STATE_DIR=/state` (production mount):

- `/state/logs/scheduler.log`, `cron.log`
- `/state/logs/jobs/<job-name>.log` — full job stdout
- `/state/logs/llm.jsonl` — structured LLM decisions (prompt, response, verdict, latency)
- Also: `docker logs aitrader`

Set `LOG_LLM_PROMPTS=0` to disable `llm.jsonl`. Verdicts still go to DB `llm_review_log`.

## Kill switch

Pause entries via `ai_overseer/ai_gate.json` (or under `/state/ai_overseer/` in the container):

```json
{"script_paused": true, "reason": "manual halt"}
```

Exits still run. Resume by deleting the file or setting `script_paused: false`.
