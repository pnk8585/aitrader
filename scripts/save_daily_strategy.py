#!/usr/bin/env python3
"""
Save daily strategy JSON from stdin to daily_strategy.json with validation.

Usage: cat /tmp/strategy.json | python3 save_daily_strategy.py

Validates schema, enums, and types. Server-stamps date/generated_at so
the LLM can never write shell-template strings. Strips ```json fences.

On success, prints a confirmation line suitable for Telegram delivery.
On failure, exits non-zero so the old strategy file is preserved.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone

OUTPUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "daily_strategy.json")
OUTPUT_PATH = os.path.normpath(OUTPUT_PATH)

REQUIRED_KEYS = [
    "date", "generated_at", "market_regime", "regime_explanation",
    "risk_level", "btc_regime", "max_daily_buys",
    "pullback_adjustment", "momentum_adjustment",
    "ai_overseer_adjustment", "geopolitical_risk",
]

VALID_ENUMS = {
    "market_regime": ["bullish", "bearish", "neutral", "mixed", "mixed_conflicting"],
    "risk_level": ["low", "medium", "high"],
    "btc_regime": ["above", "below", "neutral"],
    "geopolitical_risk": ["low", "medium", "high"],
    "pullback_adjustment": ["normal", "aggressive", "cautious", "skip"],
    "momentum_adjustment": ["normal", "cautious", "skip"],
    "ai_overseer_adjustment": ["hold", "aggressive_sell", "normal", "reduce_risk"],
}

def strip_markdown_fences(text: str) -> str:
    """Remove ```json / ``` fences that LLMs frequently wrap JSON in."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()

def validate_schema(data: dict) -> list[str]:
    """Return list of error strings. Empty = valid."""
    errors = []

    # Required keys
    for key in REQUIRED_KEYS:
        if key not in data:
            errors.append(f"Missing required key: {key}")

    if errors:
        return errors  # don't bother with enum checks if keys missing

    # Enum validation
    for key, valid_values in VALID_ENUMS.items():
        val = data.get(key)
        if val is not None and val not in valid_values:
            errors.append(
                f"Invalid {key}: '{val}'. Must be one of {valid_values}")

    # max_daily_buys: must be int >= 0
    mdb = data.get("max_daily_buys")
    if mdb is not None:
        try:
            mdb_int = int(mdb)
            if mdb_int < 0:
                errors.append(f"max_daily_buys must be >= 0, got {mdb_int}")
            data["max_daily_buys"] = mdb_int  # normalize to int
        except (ValueError, TypeError):
            errors.append(f"max_daily_buys must be an integer, got {type(mdb).__name__}: {mdb}")

    # focus_pairs / avoid_pairs: must be lists if present
    for key in ("focus_pairs", "avoid_pairs"):
        val = data.get(key)
        if val is not None and not isinstance(val, list):
            errors.append(f"{key} must be a list, got {type(val).__name__}")

    return errors

def main():
    raw = sys.stdin.read()
    if not raw.strip():
        print("ERROR: empty input", file=sys.stderr)
        sys.exit(1)

    # Strip markdown fences
    cleaned = strip_markdown_fences(raw)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    # Server-stamp date/generated_at (LLM can't be trusted with these)
    now_utc = datetime.now(timezone.utc)
    data["date"] = now_utc.strftime("%Y-%m-%d")
    data["generated_at"] = now_utc.isoformat()

    # Validate
    errors = validate_schema(data)
    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        print(f"VALIDATION FAILED: {len(errors)} error(s) — old strategy preserved", file=sys.stderr)
        sys.exit(1)

    # Atomic write
    tmp_path = OUTPUT_PATH + ".tmp." + str(os.getpid())
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, OUTPUT_PATH)
    except OSError as e:
        print(f"ERROR: write failed: {e}", file=sys.stderr)
        sys.exit(1)

    # Success — deliverable output for Telegram
    adj_parts = []
    for key in ("pullback_adjustment", "momentum_adjustment", "ai_overseer_adjustment"):
        if key in data:
            adj_parts.append(f"{key}={data[key]}")
    print(
        f"📋 Strategy saved: {data['date']} | "
        f"regime={data['market_regime']} | "
        f"btc={data['btc_regime']} | "
        f"buys={data['max_daily_buys']} | "
        f"{' | '.join(adj_parts)}"
    )

if __name__ == "__main__":
    main()
