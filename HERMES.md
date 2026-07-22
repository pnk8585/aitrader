# HERMES.md — Project Entry Point

## Overview
Multi-strategy automated trading system (AITrader) for Kraken crypto and Alpaca US stocks.

## Architecture

```
AITrader Orchestrator (cron every 1m)
├─ aitrader_orchestrator.py     ← reads registry, spawns due scripts
├─ aitrader_registry.py         ← atomic JSON state per script
│
├─ kraken-pullback (LIVE, 5m)  ← pullback entries + LLM review
├─ kraken-momentum (PAPER, 5m) ← momentum entries + LLM review
├─ position-monitor (LIVE, 2h) ← checks positions, LLM sell/hold
└─ alpaca-stocks (LIVE, 5m)    ← stock momentum + LLM review
```

**LLM evaluation** is synchronous — every buy/sell decision goes through `traders/common/llm_review.py` which:
- Queries DB for price context (1h/6h/24h, BTC, 24h range)
- Fetches recent news headlines (DuckDuckGo, 4s timeout)
- Calls DeepSeek v4 via local LiteLLM proxy
- Logs every verdict to `llm_review_log` table for later accuracy analysis

**Orchestrator modes:** `live` (real orders) | `paper` (simulated, DB-logged) | `paused`

## Project Info

| Topic | File | Purpose |
|-------|------|---------|
| Orchestrator | `aitrader_orchestrator.py` | Daemon — spawns scripts when due |
| Registry | `aitrader_orchestrator.json` | Script config (mode, interval, state) |
| LLM review | `traders/common/llm_review.py` | Sync trade evaluation + news + price context |
| Exchange utils | `traders/common/exchange.py` | Paper/live order routing |
| Gates | `traders/common/gates.py` | Safety pause (BTC drawdown, manual halt) |
| DB prices | `traders/extreme/db_prices.py` | asset_prices queries |
| Position monitor | `position_monitor.py` | Checks open positions, LLM exits |
| Pullback strategy | `traders/strategies/pullback/` | Config, signals, exits |
| Momentum strategy | `traders/strategies/momentum/` | Config, signals, exits |
| Alpaca runner | `traders/trades/alpaca_stocks.py` | Stock momentum entries |

## Guidelines
- **Write**: Save new info to the correct file — same one you found it in.
- **Secrets/keys**: Never in project files — Bitwarden only (`DEEPSEEK_API_KEY`, `KRAKEN_API_KEY`, `ALPACA_API_KEY`, DB creds).
- **Orchestrator config**: Edit `aitrader_orchestrator.json` to change modes/intervals.
- **LLM model**: `deepseek-v4-flash` via LiteLLM at `localhost:4000`.

## Docker

The admin UI runs in a container; the orchestrator stays on the host.

```bash
# One-time: populate shared state dir
bash scripts/init_state_dir.sh

# Edit /home/pank/docker-data/aitrader/.env — set DB_HOST=host.docker.internal
# (or the host LAN IP) so the container can reach the host's Postgres + LiteLLM.

docker compose up -d --build
# UI at http://localhost:9237
# Health: curl http://localhost:9237/healthz
```

The container mounts `/home/pank/docker-data/aitrader` as `/state`. Mode changes
in the UI write to `registry.json` there; the host orchestrator picks them up on
its next tick. No scheduler runs inside the container.
