# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# Trading Bot — Project Instructions

## Purpose
Sentiment-driven paper trading bot. Claude Code reads live news from Reddit and RSS feeds, scores sentiment, and executes trades on Alpaca paper trading — no Python runtime or separate AI API key required.

## How It Works

Claude Code **is** the intelligence. Each run:
1. Fetches news from Reddit (free JSON API) + Yahoo Finance / Google News RSS
2. Scores sentiment per ticker (-10 to +10)
3. Executes buy/sell via Alpaca REST API (curl)

## Files

| File | Role |
|------|------|
| `trading_prompt.md` | Bot instructions — what Claude does each run (edit this to change behaviour) |
| `config.md` | Watchlist, thresholds, trade amount — edit freely |
| `run_bot.sh` | Runner — loads `.env`, invokes `claude -p` |
| `.env` | Alpaca credentials — **never commit** |
| `.env.example` | Template for required env vars |

Old Python scripts (`bot.py`, `analyzer.py`, `config.py`) are superseded but left in place.

## Running

```bash
# One-off run
./run_bot.sh

# Scheduled — every 5 minutes via cron
*/5 * * * * cd PROJECT_ROOT && ./run_bot.sh >> logs/bot.log 2>&1
```

## Required Setup

1. Copy `.env.example` to `.env` and fill in Alpaca paper trading keys
2. Get keys from https://app.alpaca.markets → Paper Trading → API Keys
3. No other API keys needed — Reddit and RSS are free without authentication

## Key Rules
- **Paper trading only** — base URL is always `https://paper-api.alpaca.markets`
- `.env` must never be committed
- No shorting — SELL only liquidates an existing position
- Crypto trades 24/7; stocks only when `is_open: true` from Alpaca clock endpoint
- Crypto symbols: strip "/" for Alpaca API (BTC/USD → BTCUSD)

## Customisation
- Change watchlist, thresholds, or trade amount: edit `config.md`
- Change bot behaviour or decision logic: edit `trading_prompt.md`
- Add news sources: add URLs to the fetch section in `trading_prompt.md`
