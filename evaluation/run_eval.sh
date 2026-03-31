#!/bin/bash
# Portfolio Evaluation Runner
#
# Fetches live portfolio from Alpaca paper account, then runs all 10 analyst
# personalities and produces a master strategy brief.
#
# Usage:
#   ./evaluation/run_eval.sh                    — use default runner (claude)
#   EVAL_RUNNER=opencode ./evaluation/run_eval.sh
#
# Output: evaluation/reports/YYYY-MM-DD/

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

# ---------------------------------------------------------------------------
# Load credentials
# ---------------------------------------------------------------------------
ENV_FILE="$ROOT_DIR/.env.paper"
if [ ! -f "$ENV_FILE" ]; then
  ENV_FILE="$ROOT_DIR/.env"
fi
if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: No .env.paper or .env file found."
  exit 1
fi

set -a
source "$ENV_FILE"
set +a

API_KEY="${ALPACA_API_KEY}"
SECRET_KEY="${ALPACA_SECRET_KEY}"
BASE_URL="${ALPACA_BASE_URL:-https://paper-api.alpaca.markets}"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] === Portfolio Evaluation Start ==="
echo "[$(date '+%Y-%m-%d %H:%M:%S')] API: $BASE_URL"

# ---------------------------------------------------------------------------
# Fetch live portfolio data
# ---------------------------------------------------------------------------
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Fetching account and positions from Alpaca..."

ACCOUNT_JSON=$(curl -s \
  -H "APCA-API-KEY-ID: $API_KEY" \
  -H "APCA-API-SECRET-KEY: $SECRET_KEY" \
  "$BASE_URL/v2/account")

POSITIONS_JSON=$(curl -s \
  -H "APCA-API-KEY-ID: $API_KEY" \
  -H "APCA-API-SECRET-KEY: $SECRET_KEY" \
  "$BASE_URL/v2/positions")

ORDERS_JSON=$(curl -s \
  -H "APCA-API-KEY-ID: $API_KEY" \
  -H "APCA-API-SECRET-KEY: $SECRET_KEY" \
  "$BASE_URL/v2/orders?status=all&limit=20")

# Check for API errors
if echo "$ACCOUNT_JSON" | grep -q '"code"'; then
  echo "ERROR: Alpaca API returned an error:"
  echo "$ACCOUNT_JSON"
  exit 1
fi

# ---------------------------------------------------------------------------
# Prepare output directory
# ---------------------------------------------------------------------------
REPORT_DATE=$(date '+%Y-%m-%d')
REPORT_DIR="$SCRIPT_DIR/reports/$REPORT_DATE"
mkdir -p "$REPORT_DIR"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Reports will be saved to: $REPORT_DIR"

# Save raw portfolio snapshot
cat > "$REPORT_DIR/portfolio_snapshot.json" <<EOF
{
  "fetched_at": "$(date -u '+%Y-%m-%dT%H:%M:%SZ')",
  "account": $ACCOUNT_JSON,
  "positions": $POSITIONS_JSON,
  "recent_orders": $ORDERS_JSON
}
EOF

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Portfolio snapshot saved."

# ---------------------------------------------------------------------------
# Build task message
# ---------------------------------------------------------------------------
PORTFOLIO_VALUE=$(echo "$ACCOUNT_JSON" | grep -o '"portfolio_value":"[^"]*"' | cut -d'"' -f4)
CASH=$(echo "$ACCOUNT_JSON" | grep -o '"cash":"[^"]*"' | cut -d'"' -f4)
POSITION_COUNT=$(echo "$POSITIONS_JSON" | grep -o '"symbol"' | wc -l | tr -d ' ')

TASK_MESSAGE="Run the full 10-personality portfolio evaluation as described in your instructions.

## Live Portfolio Data ($(date '+%Y-%m-%d'))

**Account Summary:**
- Portfolio value: \$$PORTFOLIO_VALUE
- Cash: \$$CASH
- Open positions: $POSITION_COUNT

**Raw JSON snapshot:** $REPORT_DIR/portfolio_snapshot.json

**Investor profile:** $ROOT_DIR/evaluation/investor_profile.md

**Report output directory:** $REPORT_DIR

**Credentials (.env.paper):** $ENV_FILE

Begin with Phase 1 (McKinsey Macro + Bridgewater Risk) and work through all 5 phases. Save each report to the output directory before proceeding to the next. Finish with the master brief at $REPORT_DIR/00_master_brief.md."

# ---------------------------------------------------------------------------
# Run evaluation
# ---------------------------------------------------------------------------
EVAL_RUNNER="${EVAL_RUNNER:-claude}"
EVAL_MODEL="${EVAL_MODEL:-claude-sonnet-4-6}"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Runner: $EVAL_RUNNER | Model: $EVAL_MODEL"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting evaluation (this will take several minutes)..."

case "$EVAL_RUNNER" in
  claude)
    claude -p "$TASK_MESSAGE" \
      --system-prompt-file "$SCRIPT_DIR/eval_prompt.md" \
      --model "$EVAL_MODEL"
    ;;
  opencode)
    opencode run --dir "$ROOT_DIR" \
      -m "$EVAL_MODEL" \
      -f "$SCRIPT_DIR/eval_prompt.md" \
      -- "$TASK_MESSAGE"
    ;;
  *)
    echo "ERROR: unknown runner '$EVAL_RUNNER'. Must be 'claude' or 'opencode'."
    exit 1
    ;;
esac

echo "[$(date '+%Y-%m-%d %H:%M:%S')] === Evaluation Complete ==="
echo "Reports saved to: $REPORT_DIR"
echo ""
echo "Master brief: $REPORT_DIR/00_master_brief.md"
