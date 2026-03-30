#!/bin/bash
# Trading bot runner — two-stage pipeline
#
# Usage:
#   ./run_bot.sh          — run one cycle and exit
#   ./run_bot.sh --loop   — run continuously (Ctrl+C to stop)
#
# Configure runners and models in .env (see .env.example for all options).
#
# Quick combos (set NEWS_RUNNER / TRADER_RUNNER):
#   both opencode   → NEWS_RUNNER=opencode  TRADER_RUNNER=opencode   (default)
#   both claude     → NEWS_RUNNER=claude    TRADER_RUNNER=claude
#   opencode + claude → NEWS_RUNNER=opencode  TRADER_RUNNER=claude
#   claude + opencode → NEWS_RUNNER=claude    TRADER_RUNNER=opencode

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

# ---------------------------------------------------------------------------
# run_stage <runner> <model> <agent_file> <task>
#
#   runner     — "opencode" or "claude"
#   model      — model ID (opencode) or claude model alias/ID (claude)
#   agent_file — path to .md file used as system prompt / agent instructions
#   task       — task message
# ---------------------------------------------------------------------------
run_stage() {
  local runner="$1"
  local model="$2"
  local agent_file="$3"
  local task="$4"

  case "$runner" in
    claude)
      claude -p "$task" \
        --system-prompt-file "$agent_file" \
        --model "$model"
      ;;
    opencode)
      opencode run --dir "$SCRIPT_DIR" \
        -m "$model" \
        -f "$agent_file" \
        -- "$task"
      ;;
    *)
      echo "ERROR: unknown runner '$runner'. Must be 'claude' or 'opencode'."
      exit 1
      ;;
  esac
}

run_cycle() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] === Trading cycle start ==="

  local news_runner="${NEWS_RUNNER:-opencode}"
  local news_model="${NEWS_MODEL:-kimi-for-coding/k2p5}"
  local trader_runner="${TRADER_RUNNER:-opencode}"
  local trader_model="${TRADER_MODEL:-kimi-for-coding/k2p5}"

  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Stage 1: Fetching news  [runner=$news_runner model=$news_model]"
  run_stage "$news_runner" "$news_model" \
    "$SCRIPT_DIR/agents/news-fetcher.md" \
    "Fetch the latest financial news for these tickers: $TICKERS. Write results to news_cache.md."

  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Stage 2: Analysing and trading  [runner=$trader_runner model=$trader_model]"
  run_stage "$trader_runner" "$trader_model" \
    "$SCRIPT_DIR/agents/trader.md" \
    "Execute the full trading cycle as described in trading_prompt.md."

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
