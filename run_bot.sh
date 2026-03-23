#!/bin/bash
# Trading bot runner — loads .env and invokes Claude Code non-interactively
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

# Load env vars
set -a
source .env
set +a

# Create logs directory if it doesn't exist
mkdir -p "${LOG_DIR:-./logs}"

run_cycle() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting trading bot cycle..."
  claude -p "$(cat trading_prompt.md)"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Cycle complete."
}

if [ "$1" == "--loop" ]; then
  echo "Loop mode — press Ctrl+C to stop."
  trap 'echo ""; echo "Bot stopped."; exit 0' INT
  while true; do
    run_cycle
    echo "Sleeping ${TRADING_CYCLE_SECONDS:-300}s..."
    sleep "${TRADING_CYCLE_SECONDS:-300}"
  done
else
  run_cycle
fi
