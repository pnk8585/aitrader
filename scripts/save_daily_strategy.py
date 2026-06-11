#!/usr/bin/env python3
"""Save daily strategy JSON to the aitrader directory."""
import json
import sys
import os

def main():
    data = sys.stdin.read()
    if not data.strip():
        print("No input received", file=sys.stderr)
        sys.exit(1)
    try:
        strategy = json.loads(data)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    out_path = "/home/pank/projects/aitrader/daily_strategy.json"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(strategy, f, indent=2, ensure_ascii=False)
    print(f"Saved strategy to {out_path}")

if __name__ == "__main__":
    main()
