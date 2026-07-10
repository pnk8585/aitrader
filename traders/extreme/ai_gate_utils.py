"""Backwards-compatible re-export — use traders.common.gates instead."""

import os
import sys

_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from traders.common.gates import (  # noqa: F401
    BTC_RECOVERY_FACTOR,
    RECOVERY_THRESHOLD,
    check_gate,
    load_ai_gates,
    signal_architect_rethink,
)
