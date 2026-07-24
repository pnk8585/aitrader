"""Project paths and environment loading.

In Docker, set AITRADER_STATE_DIR=/state (bind-mounted). Logs, locks, and
gates live under that durable volume so they survive image redeploys.
"""

import os

from dotenv import load_dotenv

# traders/common/config.py -> repo root is two levels up
ROOT_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))

# Durable state (container volume) or fall back to repo root for host runs
STATE_DIR = os.path.normpath(os.environ.get("AITRADER_STATE_DIR") or ROOT_DIR)

ENV_PATH = os.path.join(STATE_DIR, ".env")
if not os.path.isfile(ENV_PATH):
    ENV_PATH = os.path.join(ROOT_DIR, ".env")

LOG_DIR = os.path.join(STATE_DIR, "logs")
AI_GATE_FILE = os.path.join(STATE_DIR, "ai_overseer", "ai_gate.json")
# Fall back to repo path if gate was never migrated to state
if not os.path.isfile(AI_GATE_FILE):
    _repo_gate = os.path.join(ROOT_DIR, "ai_overseer", "ai_gate.json")
    if os.path.isfile(_repo_gate):
        AI_GATE_FILE = _repo_gate

ARCHITECT_TRIGGER_FILE = os.path.join(STATE_DIR, ".trigger_architect_rethink")

load_dotenv(dotenv_path=ENV_PATH)

DEBUG = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")
DRY_RUN = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")
ALPACA_PAPER = os.getenv("ALPACA_PAPER", "").lower() in ("1", "true", "yes")

ALPACA_LIVE_URL = "https://api.alpaca.markets"
ALPACA_PAPER_URL = "https://paper-api.alpaca.markets"
ALPACA_BASE_URL = ALPACA_PAPER_URL if ALPACA_PAPER else ALPACA_LIVE_URL
ALPACA_DATA_URL = "https://data.alpaca.markets"


def ensure_log_dir():
    os.makedirs(LOG_DIR, exist_ok=True)
