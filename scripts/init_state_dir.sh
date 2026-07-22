#!/usr/bin/env bash
# One-shot: populate /home/pank/docker-data/aitrader for container+host state sharing.
# Idempotent — safe to run multiple times.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
STATE_DIR="/home/pank/docker-data/aitrader"

mkdir -p "$STATE_DIR/logs"

# Copy old-style registry
if [ -f "$REPO_DIR/aitrader_orchestrator.json" ]; then
    cp -n "$REPO_DIR/aitrader_orchestrator.json" "$STATE_DIR/registry.json" 2>/dev/null || true
fi

# Copy .env
if [ -f "$REPO_DIR/.env" ]; then
    cp -n "$REPO_DIR/.env" "$STATE_DIR/.env" 2>/dev/null || true
fi

echo "STATE_DIR ready: $STATE_DIR"
echo "  - registry.json: $([ -f "$STATE_DIR/registry.json" ] && echo 'YES' || echo 'NO')"
echo "  - .env:          $([ -f "$STATE_DIR/.env" ] && echo 'YES' || echo 'NO')"
echo "  - logs/          $([ -d "$STATE_DIR/logs" ] && echo 'YES' || echo 'NO')"
