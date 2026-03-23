#!/bin/bash
# Trading bot runner — two-stage pipeline:
#   Stage 1: opencode fetches news (cheap, fast)
#   Stage 2: claude makes decisions and executes trades
#
# Usage:
#   ./run_bot.sh          — run one cycle and exit
#   ./run_bot.sh --loop   — run continuously (Ctrl+C to stop)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -f .env ]; then
  echo "ERROR: .env file not found. Copy .env.example and fill in your Alpaca keys."
  exit 1
fi

set -a
source .env
set +a

mkdir -p "${LOG_DIR:-./logs}"

TICKERS="TSLA, NVDA, AMD, MSTR, COIN, SMCI, PLTR, ROKU, SNAP, SHOP"

run_cycle() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] === Trading cycle start ==="

  # Stage 1: Fetch news with opencode (cheap model)
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Stage 1: Fetching news..."
  opencode run --dir "$SCRIPT_DIR" \
    -m kimi-for-coding/k2p5 \
    -f "$SCRIPT_DIR/agents/news-fetcher.md" \
    -- "Fetch the latest financial news for these tickers: $TICKERS. Write results to news_cache.md."

  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Stage 2: Analysing and trading..."

  # Stage 2: Analyse and trade with claude
  claude -p "$(cat trading_prompt.md)"

  echo "[$(date '+%Y-%m-%d %H:%M:%S')] === Cycle complete ==="
}

if [ "$1" == "--loop" ]; then
  echo "Loop mode active — press Ctrl+C to stop."
  trap 'echo ""; echo "Bot stopped."; exit 0' INT
  while true; do
    run_cycle
    echo "Sleeping ${TRADING_CYCLE_SECONDS:-60}s..."
    sleep "${TRADING_CYCLE_SECONDS:-60}"
  done
else
  run_cycle
fi
