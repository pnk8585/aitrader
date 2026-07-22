"""In-container scheduler loop. Runs tick() every 60 seconds."""

import time
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from app.db import get_conn
from app.cron_orchestrator import tick

while True:
    try:
        with get_conn() as conn:
            result = tick(conn)
            if result["ran"]:
                print(f"tick: ran={result['ran']}")
    except Exception as e:
        print(f"scheduler error: {e}", file=sys.stderr)
    time.sleep(60)
