"""Project paths and environment loading."""

import os

from dotenv import load_dotenv

# traders/common/config.py -> repo root is two levels up
ROOT_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))

ENV_PATH = os.path.join(ROOT_DIR, ".env")
LOG_DIR = os.path.join(ROOT_DIR, "logs")
AI_GATE_FILE = os.path.join(ROOT_DIR, "ai_overseer", "ai_gate.json")
DAILY_STRATEGY_PATH = os.path.join(ROOT_DIR, "daily_strategy.json")
PENDING_REVIEW_FILE = os.path.join(ROOT_DIR, "ai_overseer", "pending_review.json")
PENDING_LOCK_FILE = os.path.join(ROOT_DIR, "ai_overseer", ".pending_review.lock")
ARCHITECT_TRIGGER_FILE = os.path.join(ROOT_DIR, ".trigger_architect_rethink")

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