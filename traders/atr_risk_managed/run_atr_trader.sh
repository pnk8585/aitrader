#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/../.."

set -a
source .env
set +a

TRADER_RUNNER="${TRADER_RUNNER:-opencode}"

TRADING_CYCLE_SECONDS="${TRADING_CYCLE_SECONDS:-300}"

TRADER_NAME="atr_risk_managed"
LOG_FILE="logs/trades-$(date +%Y-%m-%d).jsonl"
mkdir -p logs

while true; do
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] ====== ATR Risk-Managed Trading Cycle Started ======" | tee -a "$LOG_FILE"
  
  if [ "$TRADER_RUNNER" = "claude" ]; then
    claude traders/$TRADER_NAME/atr_trading_prompt.md
  elif [ "$TRADER_RUNNER" = "opencode" ]; then
    opencode traders/$TRADER_NAME/atr_trading_prompt.md
  else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Unknown TRADER_RUNNER: $TRADER_RUNNER" | tee -a "$LOG_FILE"
    exit 1
  fi
  
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] ====== Trading Cycle Completed ======" | tee -a "$LOG_FILE"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Waiting ${TRADING_CYCLE_SECONDS}s before next cycle..." | tee -a "$LOG_FILE"
  
  sleep "$TRADING_CYCLE_SECONDS"
done