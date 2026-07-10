"""Backwards-compatible re-export — use traders.common.gates instead."""

from traders.common import bootstrap  # noqa: F401

from traders.common.gates import (  # noqa: F401
    BTC_RECOVERY_FACTOR,
    RECOVERY_THRESHOLD,
    check_gate,
    load_ai_gates,
    signal_architect_rethink,
)
