#!/bin/bash
# Trading bot runner — loads .env and invokes Claude Code non-interactively

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

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting trading bot run..."

claude -p "$(cat trading_prompt.md)"
