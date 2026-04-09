# AGENTS.md

Guidelines for agentic coding agents working in this repository.

## Project Overview

Autonomous news-driven stock trading bot. AI agents fetch financial news, analyze sentiment, and execute paper trades via Alpaca REST API. The intelligence is provided by AI agents (opencode or claude), not traditional ML models.

## Build / Run Commands

```bash
# Run single trading cycle
./run_bot.sh

# Run aggressive trader (short-term 2% profit-taking mode)
./run_aggressive.sh

# Run extreme trader (pure momentum, NO news)
./run_extreme.sh

# Run continuously (Ctrl+C to stop)
./run_bot.sh --loop
./run_aggressive.sh --loop
./run_extreme.sh --loop

# Run portfolio evaluation
./evaluation/run_eval.sh

# Run evaluation with opencode
EVAL_RUNNER=opencode ./evaluation/run_eval.sh
```

## Python Commands

No package manager configured. Dependencies are minimal:
- `alpaca-trade-api` — Alpaca SDK
- `python-dotenv` — Environment loading
- Standard library: `os`, `json`, `re`, `time`

```bash
# Install dependencies (if needed)
pip install alpaca-trade-api python-dotenv

# Run Python modules directly
python bot.py
python -c "from analyzer import analyze_sentiment; print(analyze_sentiment('good news', 'AAPL'))"
```

## Testing

No test framework configured. When adding tests:
- Use `pytest` as the test framework
- Place tests in `tests/` directory
- Name test files `test_*.py`

```bash
# Run all tests (once configured)
pytest

# Run single test file
pytest tests/test_analyzer.py

# Run single test function
pytest tests/test_analyzer.py::test_analyze_sentiment
```

## Linting / Formatting

No linter configured. Recommended:
- `ruff` for linting and formatting
- `mypy` for type checking

```bash
# Lint (once configured)
ruff check .

# Format (once configured)
ruff format .

# Type check (once configured)
mypy .
```

## Code Style

### Python

- **Imports**: Standard library first, third-party second, local last. Blank line between groups.
- **Naming**: `snake_case` for functions/variables, `UPPER_CASE` for constants, `PascalCase` for classes.
- **Strings**: Use double quotes for strings.
- **Functions**: Keep functions small and focused. Add docstrings for public functions.
- **Error handling**: Use explicit checks, raise exceptions for unrecoverable errors.
- **Comments**: Minimal. Code should be self-documenting. No inline comments explaining obvious code.

```python
# Good import order
import os
import json

import alpaca_trade_api as tradeapi
from dotenv import load_dotenv

from analyzer import analyze_sentiment
from config import BUY_THRESHOLD
```

### Shell Scripts

- **Error handling**: Always start with `set -e` to exit on error.
- **Path resolution**: Use `SCRIPT_DIR` pattern for reliable relative paths.
- **Environment**: Load `.env` with `set -a; source .env; set +a`.
- **Functions**: Use `local` for function-scoped variables.
- **Logging**: Prefix output with timestamps: `[$(date '+%Y-%m-%d %H:%M:%S')]`.

```bash
#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

set -a
source .env
set +a
```

### Markdown (Agent Prompts)

- Files in `agents/` and `evaluation/` are AI agent system prompts.
- Use clear section headers with `##`.
- Number steps sequentially.
- Include exact API endpoints and curl examples.
- Specify output file paths explicitly.

## Project Structure

```
aitrader/
├── agents/              # AI agent system prompts
│   ├── news-fetcher.md  # Stage 1: fetch news
│   ├── trader.md        # Stage 2: execute trades
│   ├── aggressive-trader.md # Stage 2: aggressive execute trades
│   └── extreme-trader.md    # Stage 2: extreme momentum trader (no news)
├── evaluation/          # Portfolio evaluation system
│   ├── run_eval.sh      # Evaluation runner
│   ├── eval_prompt.md   # Evaluation agent prompt
│   ├── personalities/   # 10 analyst personas
│   └── reports/         # Generated reports (gitignored)
├── logs/                # JSONL trade logs (gitignored)
├── .env                 # Credentials (never commit)
├── .env.example         # Template for env vars
├── bot.py               # Legacy Python entry point
├── analyzer.py          # Sentiment analysis module
├── config.py            # Python config constants
├── config.md           # Trading config (watchlist, thresholds)
├── aggressive_trading_prompt.md # Aggressive short-term trading logic prompt for AI agent
├── extreme_trading_prompt.md    # Extreme momentum trading logic (NO news)
├── trading_prompt.md   # Full trading logic for AI agent
├── run_bot.sh          # Main runner script
├── run_aggressive.sh   # Aggressive runner script
└── run_extreme.sh      # Extreme momentum runner script (no news)
```

## Environment Variables

Required in `.env`:
- `ALPACA_API_KEY` — Alpaca API key
- `ALPACA_SECRET_KEY` — Alpaca secret key
- `ALPACA_BASE_URL` — `https://paper-api.alpaca.markets` (paper trading)

Optional:
- `NEWS_RUNNER` / `TRADER_RUNNER` — `opencode` or `claude`
- `NEWS_MODEL` / `TRADER_MODEL` — Model ID for each stage
- `MAX_POSITION_PCT` — Max notional per trade (fraction of equity)
- `MAX_SINGLE_TICKER_PCT` — Max exposure per ticker
- `TRADING_CYCLE_SECONDS` — Loop interval (default: 60)

## Key Rules

1. **Never commit `.env`** — contains API credentials.
2. **Paper trading only** — `ALPACA_BASE_URL` must point to paper API.
3. **No shorting, no leverage, no options** — long equities only.
4. **Max 5 open positions** — enforced in `trading_prompt.md`.
5. **Stop-loss at -5%** — no exceptions.
6. **Circuit breaker** — halt if drawdown exceeds 15% from peak.
7. **No trading outside market hours** — check clock before orders.

## Logging Format

Trade logs are JSONL in `logs/trades-YYYY-MM-DD.jsonl`:

```json
{"timestamp": "2026-03-31T12:00:00Z", "action": "BUY", "ticker": "TSLA", "signal_strength": "STRONG_BUY", ...}
```

Never delete log files. They are gitignored but preserved for audit.

## When Adding New Files

- Python modules: follow existing import patterns, add to `.gitignore` if generated.
- Agent prompts: place in `agents/` or `evaluation/personalities/`.
- Shell scripts: make executable (`chmod +x`), use `set -e`.
- Config changes: update both `config.md` and `.env.example`.
