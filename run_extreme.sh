#!/bin/bash
# Extreme momentum trading bot runner — pure price action, no news
#
# Usage:
#   ./run_extreme.sh          — run one cycle and exit
#   ./run_extreme.sh --loop   — run continuously (Ctrl+C to stop)
#
# The extreme trader:
#   - Does NOT fetch news (pure momentum strategy)
#   - Scans Alpaca for stocks with strong intraday momentum
#   - Buys top momentum candidates
#   - Sells immediately at 2% profit
#   - Falls back to crypto if no stock opportunities
#
# Configure runner and model in .env (see .env.example for all options).

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

EXTREME_TRADER_INTERVAL_SECONDS="${EXTREME_TRADER_INTERVAL_SECONDS:-60}"  # 1 minute (fast rotation)

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
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] === Extreme momentum cycle start ==="

  # NO NEWS FETCHING — extreme trader is pure momentum-based
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Mode: EXTREME MOMENTUM (no news)"

  # Run extreme trader (default: opencode with zai-coding-plan/glm-5.1)
  local trader_runner="${EXTREME_TRADER_RUNNER:-opencode}"
  local trader_model="${EXTREME_TRADER_MODEL:-zai-coding-plan/glm-5.1}"

  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Running extreme momentum trader  [runner=$trader_runner model=$trader_model]"
  run_stage "$trader_runner" "$trader_model" \
    "$SCRIPT_DIR/agents/extreme-trader.md" \
    "Execute the full trading cycle as described in extreme_trading_prompt.md."

  echo "[$(date '+%Y-%m-%d %H:%M:%S')] === Extreme cycle complete ==="
}

if [ "$1" == "--loop" ]; then
  echo "Loop mode active — press Ctrl+C to stop."
  echo "Schedule: Extreme trader every ${EXTREME_TRADER_INTERVAL_SECONDS}s (fast momentum rotation)"
  trap 'echo ""; echo "Bot stopped."; exit 0' INT
  while true; do
    run_cycle
    echo "Sleeping ${EXTREME_TRADER_INTERVAL_SECONDS}s until next extreme cycle..."
    sleep "$EXTREME_TRADER_INTERVAL_SECONDS"
  done
else
  run_cycle
fi
