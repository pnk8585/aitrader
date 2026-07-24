#!/usr/bin/env python3
"""Start both the web server and cron scheduler in one container."""
import subprocess
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

# Inherit stdout/stderr — never use PIPE here. An undrained pipe fills (~64KB)
# and blocks scheduler.py forever (ticks stop while /healthz still passes).
scheduler = subprocess.Popen([sys.executable, "scheduler.py"])

try:
    subprocess.run([
        sys.executable, "-m", "uvicorn", "app.main:app",
        "--host", "0.0.0.0", "--port", "9237",
    ])
finally:
    scheduler.terminate()
    try:
        scheduler.wait(timeout=10)
    except subprocess.TimeoutExpired:
        scheduler.kill()
        scheduler.wait()
