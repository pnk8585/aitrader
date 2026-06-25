"""Shared AI gate utilities with condition-based auto-resume.

Provides:
  - load_ai_gates()         — backwards-compatible gate loader
  - check_gate(db_conn)     — returns (paused: bool, reason_or_none: str|None)
                              If the gate was paused and the trigger condition
                              has reversed, auto-clears the gate so the caller
                              can resume entries.
"""

import os
import json
from datetime import datetime, timezone

# --- Paths ----------------------------------------------------------------
# Scripts import this module from traders/crypto_trades/ or traders/extreme/,
# so resolve the repo root relative to THIS file (traders/extreme/ -> ../..).
_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.normpath(os.path.join(_HERE, "..", ".."))
AI_GATE_FILE = os.path.join(ROOT_DIR, "ai_overseer", "ai_gate.json")
ARCHITECT_TRIGGER_FILE = os.path.join(ROOT_DIR, ".trigger_architect_rethink")

# --- BTC recovery threshold -----------------------------------------------
# Require BTC > 6h average multiplied by this factor (0.99 = within 1% of avg).
# Matches the Overseer's BTC_DEVIATION_PAUSE: only treat as "recovered" when
# BTC is above the same threshold that triggers a pause.
BTC_RECOVERY_FACTOR = 0.99
RECOVERY_THRESHOLD = 3   # consecutive checks before auto-clear


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def load_ai_gates():
    """Read AI gate conditions set by ai_overseer.

    Backwards-compatible with the same signature the kraken bots currently
    define locally.  Returns a dict with safe defaults.
    """
    default = {"script_paused": False, "consult_on_entry": False, "reason": None}
    if not os.path.exists(AI_GATE_FILE):
        return default
    try:
        with open(AI_GATE_FILE) as f:
            gates = json.load(f)
        return {**default, **gates}
    except (json.JSONDecodeError, IOError):
        return default


def check_gate(db_conn, daily_strategy=None):
    """Check whether the AI gate blocks entries.

    Returns
    -------
    (paused: bool, reason_or_msg: str | None)
        paused=True  -> the gate is active and entries should be skipped.
        paused=False, reason=None -> gate is open, proceed normally.
        paused=False, reason="auto-resumed: ..." -> gate was open **and** we
                        just auto-cleared a stale pause.  Callers should log
                        the message to notify the user.
    """
    gates = load_ai_gates()
    if not gates.get("script_paused"):
        return False, None  # gate is open

    reason = gates.get("reason", "no reason given")

    # --- Attempt condition-based auto-resume ------------------------------
    trigger = gates.get("trigger") or {}
    trigger_type = trigger.get("type", "")

    recovered = False
    if trigger_type == "btc_below_avg":
        recovered = _check_btc_recovery(db_conn)
    elif trigger_type:
        # Unknown trigger type — safe fallback: keep paused
        return True, reason

    if recovered:
        # Increment recovery counter
        count = gates.get("recovery_count", 0) + 1
        do_clear = count >= gates.get("recovery_threshold", RECOVERY_THRESHOLD)
        gates["recovery_count"] = count

        if do_clear:
            _write_clear_gate()
            msg = f"auto-resumed after {count} recovery checks: {trigger_type}"
            return False, msg
        else:
            # Persist the updated counter so the next bot cycle sees it
            _write_partial(gates)
            return True, f"{reason} (recovery {count}/{gates.get('recovery_threshold', RECOVERY_THRESHOLD)})"
    else:
        # Not recovered — reset counter so we require N *consecutive* checks
        if gates.get("recovery_count", 0) > 0:
            gates["recovery_count"] = 0
            _write_partial(gates)
        return True, reason


def signal_architect_rethink(trigger_reason):
    """Write a flag file so the market-move-watchdog can trigger a re-evaluation."""
    try:
        with open(ARCHITECT_TRIGGER_FILE, "w") as f:
            f.write(json.dumps({
                "triggered_at": datetime.now(timezone.utc).isoformat(),
                "reason": trigger_reason,
            }))
    except IOError:
        pass  # non-fatal


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _check_btc_recovery(db_conn):
    """Check if BTC price is within the 1% threshold above the 6h average.

    Uses the last 72 ticks of asset_prices (≈6h at 5-min granularity).
    Returns True if current price > avg * BTC_RECOVERY_FACTOR (0.99).
    This means BTC is at least above the 1%-below-avg zone.
    """
    if db_conn is None:
        return False
    try:
        cur = db_conn.cursor()
        cur.execute(
            "SELECT price FROM asset_prices "
            "WHERE exchange='kraken' AND symbol='BTC' "
            "ORDER BY timestamp DESC LIMIT 72")
        rows = cur.fetchall()
    except Exception:
        return False

    if not rows or len(rows) < 12:
        return False  # not enough data — keep paused

    prices = [float(r[0]) for r in rows]
    current = prices[0]
    avg_6h = sum(prices) / len(prices)
    return current > avg_6h * BTC_RECOVERY_FACTOR


def _write_clear_gate():
    """Set script_paused to False and clear recovery state."""
    payload = {
        "script_paused": False,
        "consult_on_entry": False,
        "reason": "Auto-resume: condition recovered",
        "paused_since": None,
        "trigger": None,
        "recovery_count": 0,
        "recovery_threshold": RECOVERY_THRESHOLD,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write(payload)


def _write_partial(gates):
    """Persist recovery_count back to the gate file (gate stays paused)."""
    gates["updated_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_write(gates)


def _atomic_write(data):
    """Write gate JSON atomically using temp-file + rename."""
    import tempfile
    try:
        os.makedirs(os.path.dirname(AI_GATE_FILE), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(AI_GATE_FILE))
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, AI_GATE_FILE)
    except (IOError, OSError):
        pass
