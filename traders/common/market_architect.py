#!/usr/bin/env python3
"""Market Architect — on-demand strategy provider for crypto trading bots.

Replaces the old daily cron (daily-market-architect). Now called by trading
scripts BEFORE they evaluate a candidate. Returns the current market strategy
as a dict, regenerating if the cached JSON is older than 2 hours.

Flow:
    Candidate found → get_strategy()
      ├─ JSON < 2h old → return cached
      └─ JSON stale/missing → write trigger → return None
    Next Hermes cron tick picks up trigger → regenerates → writes JSON

Usage:
    from traders.common.market_architect import get_strategy
    strategy = get_strategy()
    if strategy:
        # Inject strategy["market_regime"], etc. into LLM prompt
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

STRATEGY_PATH = "/home/pank/projects/aitrader/daily_strategy.json"
TRIGGER_PATH = "/home/pank/projects/aitrader/.market_architect_trigger"
MAX_AGE_HOURS = 2


def get_strategy() -> dict | None:
    """Return the current market strategy dict, or None if stale/missing.

    Cached JSON is considered fresh if `generated_at` is within MAX_AGE_HOURS.
    When stale, writes a trigger file so the next Hermes tick can regenerate.
    """
    # 1) Check if cached JSON exists and is fresh
    if os.path.exists(STRATEGY_PATH):
        try:
            with open(STRATEGY_PATH) as f:
                data = json.load(f)
            gen_str = data.get("generated_at", "")
            if gen_str:
                # Parse ISO timestamp (handle both naive and aware)
                gen_ts = _parse_iso(gen_str)
                age = datetime.now(timezone.utc) - gen_ts
                if age < timedelta(hours=MAX_AGE_HOURS):
                    return data
        except (json.JSONDecodeError, KeyError, ValueError):
            pass  # fall through → trigger regeneration

    # 2) Stale or missing — request regeneration
    _request_regeneration()
    return None


def _parse_iso(ts_str: str) -> datetime:
    """Parse ISO timestamp, making naive timestamps UTC-aware."""
    dt = datetime.fromisoformat(ts_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _request_regeneration() -> None:
    """Write a trigger file so Hermes knows to regenerate the strategy."""
    os.makedirs(os.path.dirname(TRIGGER_PATH), exist_ok=True)
    with open(TRIGGER_PATH, "w") as f:
        f.write(datetime.now(timezone.utc).isoformat())


# ── CLI (for manual/manual testing) ─────────────────────────────────
if __name__ == "__main__":
    strat = get_strategy()
    if strat:
        print(f"✅ Strategy fresh: {strat['market_regime']} | pullback={strat['pullback_adjustment']} | max_buys={strat['max_daily_buys']}")
    else:
        print("⚠️  Strategy stale — regeneration triggered")
