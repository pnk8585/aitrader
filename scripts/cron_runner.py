"""Entry point for cron operations. Invoked by scheduler.py or manually.

    python -m scripts.cron_runner --mode tick      # one orchestrator tick
    python -m scripts.cron_runner --mode run-jobs  # run all due jobs once
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import get_conn
from app.cron_orchestrator import tick, run_job, list_jobs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["tick", "run-jobs"], required=True)
    args = parser.parse_args()

    with get_conn() as conn:
        if args.mode == "tick":
            result = tick(conn)
            print(f"tick: ran={result['ran']}, checked={result['checked']}, errors={result['errors']}")
        elif args.mode == "run-jobs":
            jobs = list_jobs(conn)
            for job in jobs:
                name = job["name"]
                try:
                    res = run_job(conn, name)
                    print(f"{name}: {res['status']} — {res.get('summary', '')[:120]}")
                except Exception as e:
                    print(f"{name}: error — {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
