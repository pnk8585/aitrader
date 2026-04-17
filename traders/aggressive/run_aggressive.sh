#!/bin/bash
# Aggressive trading bot runner — two-stage pipeline
#
# Usage:
#   ./run_aggressive.sh          — run one cycle and exit
#   ./run_aggressive.sh --loop   — run continuously (Ctrl+C to stop)
#
# Configure runners and models in .env (see .env.example for all options).
#
# Stages:
#   Stage 1: News fetching
#   Stage 2: Aggressive trading execution
#
# Note: Portfolio evaluation runs separately via ./evaluation/run_eval.sh

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
mkdir -p evaluation/reports

TICKERS="TSLA, NVDA, AMD, MSTR, COIN, SMCI, PLTR, ROKU, SNAP, SHOP, BTC/USD, ETH/USD"
NEWS_STATE_FILE="$SCRIPT_DIR/.news_last_run"

NEWS_INTERVAL_SECONDS=1800  # 30 minutes
AGGRESSIVE_TRADER_INTERVAL_SECONDS="${AGGRESSIVE_TRADER_INTERVAL_SECONDS:-300}"  # 5 minutes

# ---------------------------------------------------------------------------
# get_timestamp_seconds — returns current timestamp in seconds
# ---------------------------------------------------------------------------
get_timestamp_seconds() {
  date +%s
}

# ---------------------------------------------------------------------------
# should_run_news — returns 0 if news should be fetched now
# ---------------------------------------------------------------------------
should_run_news() {
  if [ ! -f "$NEWS_STATE_FILE" ]; then
    return 0
  fi

  local last_run=$(cat "$NEWS_STATE_FILE" 2>/dev/null || echo "0")
  local now=$(get_timestamp_seconds)
  local elapsed=$((now - last_run))

  if [ "$elapsed" -ge "$NEWS_INTERVAL_SECONDS" ]; then
    return 0
  fi

  return 1
}

# ---------------------------------------------------------------------------
# run_news — fetches financial news
# ---------------------------------------------------------------------------
run_news() {
  local news_runner="${NEWS_RUNNER:-opencode}"
  local news_model="${NEWS_MODEL:-kimi-for-coding/k2p5}"

  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Stage 1: Fetching news  [runner=$news_runner model=$news_model]"
  run_stage "$news_runner" "$news_model" \
    "$SCRIPT_DIR/agents/news-fetcher.md" \
    "Fetch the latest financial news for these tickers: $TICKERS. Write results to news_cache.md."

  get_timestamp_seconds > "$NEWS_STATE_FILE"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] News fetch complete"
}

# ---------------------------------------------------------------------------
# run_stage <runner> <model> <agent_file> <task>
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
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] === Aggressive trading cycle start ==="

  # Stage 1: Fetch news every 30 minutes
  if should_run_news; then
    run_news
  else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Skipping news fetch (ran within last 30 min)"
  fi

  # Stage 2: Run aggressive trader every configured interval (default 5 minutes)
  local trader_runner="${AGGRESSIVE_TRADER_RUNNER:-${TRADER_RUNNER:-opencode}}"
  local trader_model="${AGGRESSIVE_TRADER_MODEL:-${TRADER_MODEL:-kimi-for-coding/k2p5}}"

  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Stage 2: Aggressive analysing and trading  [runner=$trader_runner model=$trader_model]"
  run_stage "$trader_runner" "$trader_model" \
    "$SCRIPT_DIR/agents/aggressive-trader.md" \
    "Execute the full trading cycle as described in aggressive_trading_prompt.md."

  echo "[$(date '+%Y-%m-%d %H:%M:%S')] === Aggressive cycle complete ==="
}

if [ "$1" == "--loop" ]; then
  echo "Loop mode active — press Ctrl+C to stop."
  echo "Schedule: News every 30 min, Aggressive trader every ${AGGRESSIVE_TRADER_INTERVAL_SECONDS} sec"
  trap 'echo ""; echo "Bot stopped."; exit 0' INT
  while true; do
    run_cycle
    echo "Sleeping ${AGGRESSIVE_TRADER_INTERVAL_SECONDS}s until next aggressive trader cycle..."
    sleep "$AGGRESSIVE_TRADER_INTERVAL_SECONDS"
  done
else
  run_cycle
fi
