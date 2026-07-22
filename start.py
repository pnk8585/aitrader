#!/usr/bin/env python3
"""Start both the web server and cron scheduler in one container."""
import subprocess, sys, time, os

sys.path.insert(0, os.path.dirname(__file__))

# Start scheduler as background subprocess
scheduler = subprocess.Popen(
    [sys.executable, "scheduler.py"],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT
)

# Start uvicorn in foreground
try:
    subprocess.run([
        sys.executable, "-m", "uvicorn", "app.main:app",
        "--host", "0.0.0.0", "--port", "9237"
    ])
finally:
    scheduler.terminate()
    scheduler.wait()
