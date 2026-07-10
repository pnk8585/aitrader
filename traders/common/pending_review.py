"""Shared pending AI review file operations."""

import json
import os

from traders.common.config import PENDING_LOCK_FILE, PENDING_REVIEW_FILE
from traders.common.locks import atomic_write_json, load_json_with_defaults, with_file_lock

PENDING_REVIEW_TIMEOUT_MIN = 120

DEFAULTS = {
    "status": None,
    "bot": None,
    "symbol": None,
    "verdict": None,
    "verdict_reason": None,
    "created_at": None,
    "reviewed_at": None,
}


def load_pending_review():
    return load_json_with_defaults(PENDING_REVIEW_FILE, DEFAULTS)


def write_pending_review(data):
    atomic_write_json(PENDING_REVIEW_FILE, data, indent=2)


def clear_pending_review():
    if os.path.exists(PENDING_REVIEW_FILE):
        os.remove(PENDING_REVIEW_FILE)


def submit_candidate(pending_data) -> bool:
    """Submit under lock — returns False if another bot already pending."""

    def _do():
        if os.path.exists(PENDING_REVIEW_FILE):
            try:
                with open(PENDING_REVIEW_FILE) as f:
                    existing = json.load(f)
                if existing.get("status") == "pending":
                    return False
            except (json.JSONDecodeError, IOError):
                pass
        write_pending_review(pending_data)
        return True

    return with_file_lock(PENDING_LOCK_FILE, _do)