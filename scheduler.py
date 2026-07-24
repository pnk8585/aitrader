"""In-container scheduler loop. Runs tick() every 60 seconds."""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(__file__))

from app.db import get_conn
from app.cron_orchestrator import tick
from app.logging_setup import apply_log_level_from_settings, configure_logging, setup_logging

configure_logging()
try:
    apply_log_level_from_settings()
except Exception:
    pass  # DB may not be ready yet on cold start

log = setup_logging("scheduler")

log.info("scheduler started (tick every 60s)")

while True:
    try:
        with get_conn() as conn:
            result = tick(conn)
            if result["ran"]:
                log.info("tick ran=%s errors=%s", result["ran"], result.get("errors") or {})
            else:
                log.debug("tick idle checked=%s", result.get("checked"))
    except Exception as e:
        log.exception("scheduler error: %s", e)
    time.sleep(60)
