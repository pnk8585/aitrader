# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# Autonomous News-Driven Stock Trading Agent

## Purpose
Fully autonomous paper trading bot. Claude Code reads live financial news, scores sentiment per ticker, and executes trades via the Alpaca REST API — no separate AI API key required.

## How It Works

Claude Code **is** the intelligence. Each cycle (`run_bot.sh`):
1. Fetches news from Alpaca News API, Yahoo Finance RSS, Google News RSS, SEC EDGAR
2. Filters: discards duplicates and items older than 4 hours
3. Classifies signal strength per ticker (Strong Buy → Strong Sell)
4. Checks portfolio state, position limits, market hours
5. Executes trades via Alpaca REST API (curl)
6. Logs every decision to `./logs/trades-YYYY-MM-DD.jsonl`

## Files

| File | Role |
|------|------|
| `trading_prompt.md` | Full agent instructions — all trading logic lives here |
| `config.md` | Watchlist, thresholds, risk rules — edit to tune behaviour |
| `run_bot.sh` | Runner — loads `.env`, creates log dir, invokes `claude -p` |
| `.env` | All credentials and settings — **never commit** |
| `.env.example` | Template for all required/optional env vars |
| `logs/` | JSONL trade logs — never deleted, gitignored |

## Running

```bash
# Single cycle (manual)
./run_bot.sh

# Scheduled — every 5 minutes via cron
*/5 * * * * cd PROJECT_ROOT && ./run_bot.sh >> logs/runner.log 2>&1
```

## Required Setup

1. Copy `.env.example` → `.env` and fill in Alpaca paper trading keys
2. Get keys from https://app.alpaca.markets → Paper Trading → API Keys
3. `NEWS_API_KEY` is optional (NewsAPI.org free tier = 100 req/day)

## Key Rules (enforced in `trading_prompt.md`)
- **Paper trading only** until 5 days of reviewed logs — `ALPACA_BASE_URL=https://paper-api.alpaca.markets`
- No shorting, no leverage, no crypto, no options — equities only
- Max 10 open positions, max 15% equity in one ticker
- Stop-loss at -7%, partial profit at +15%, stale position close after 3 days
- Circuit breaker: halt new entries if portfolio drawdown exceeds 10%
- Never trade outside market hours

## Customisation
- Tune watchlist, thresholds, risk rules: edit `config.md`
- Change trading logic or news sources: edit `trading_prompt.md`
- Add env vars: update `.env` and `.env.example`
