"""Daily strategy loader — single implementation for all bots."""

import json
import sys
from datetime import datetime, timedelta

import pytz

from traders.common.config import DAILY_STRATEGY_PATH


def _normalize_symbol(raw: str, pool_bases: set[str], pair_suffix: str) -> str | None:
    if not isinstance(raw, str):
        return None
    base = raw.upper()
    if pair_suffix:
        suffix = pair_suffix.upper()
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    return base if base in pool_bases else None


def load_daily_strategy(
    *,
    pool_bases: set[str],
    adjustment_key: str | None = None,
    valid_adjustments: tuple[str, ...] = ("normal",),
    default_adjustment: str = "normal",
    pair_suffix: str = "/EUR",
) -> dict | None:
    """Load Market Architect daily strategy with staleness guard and normalization."""
    try:
        if not pool_bases:
            return None
        if not __import__("os").path.exists(DAILY_STRATEGY_PATH):
            return None
        with open(DAILY_STRATEGY_PATH) as f:
            data = json.load(f)

        date_str = data.get("date")
        if date_str:
            athens_tz = pytz.timezone("Europe/Athens")
            today_athens = datetime.now(athens_tz).strftime("%Y-%m-%d")
            cutoff = (datetime.now(athens_tz) - timedelta(days=1)).strftime("%Y-%m-%d")
            if date_str < cutoff:
                print(
                    f"WARN: Daily strategy is stale (date={date_str}, today={today_athens})",
                    file=sys.stderr,
                )
                return None

        normalized_focus = []
        normalized_avoid = []
        for pair in data.get("focus_pairs", []):
            base = _normalize_symbol(pair, pool_bases, pair_suffix)
            if base:
                normalized_focus.append(base)
        for pair in data.get("avoid_pairs", []):
            base = _normalize_symbol(pair, pool_bases, pair_suffix)
            if base:
                normalized_avoid.append(base)

        if data.get("focus_pairs") and not normalized_focus:
            print(
                f"WARN: Daily strategy focus_pairs filter ignored — "
                f"no valid pairs in {data.get('focus_pairs')}",
                file=sys.stderr,
            )

        data["focus_pairs"] = normalized_focus
        data["avoid_pairs"] = normalized_avoid

        if adjustment_key:
            adj = data.get(adjustment_key, default_adjustment)
            if adj not in valid_adjustments:
                print(
                    f"WARN: Unknown {adjustment_key} '{adj}', defaulting to '{default_adjustment}'",
                    file=sys.stderr,
                )
                data[adjustment_key] = default_adjustment

        return data
    except (json.JSONDecodeError, IOError) as e:
        print(f"WARN: Could not load daily strategy: {e}", file=sys.stderr)
        return None