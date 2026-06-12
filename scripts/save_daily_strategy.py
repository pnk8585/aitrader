#!/usr/bin/env python3
"""
Save daily strategy JSON from stdin to /home/pank/projects/aitrader/daily_strategy.json
Usage: python3 save_daily_strategy.py < /tmp/daily_strategy_raw.json
"""
import json
import sys
import os

OUTPUT_PATH = "/home/pank/projects/aitrader/daily_strategy.json"

def main():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"Saved strategy to {OUTPUT_PATH}")

if __name__ == "__main__":
    main()
