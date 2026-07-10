"""AI gate utilities with condition-based auto-resume."""

import json
import os
import tempfile
from datetime import datetime, timezone

from traders.common.config import AI_GATE_FILE, ARCHITECT_TRIGGER_FILE

BTC_RECOVERY_FACTOR = 0.99
RECOVERY_THRESHOLD = 3


def load_ai_gates():
    """Read AI gate conditions set by ai_overseer."""
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
    """Return (paused, reason_or_msg). Auto-clears stale pauses when recovered."""
    gates = load_ai_gates()
    if not gates.get("script_paused"):
        return False, None

    reason = gates.get("reason", "no reason given")
    trigger = gates.get("trigger") or {}
    trigger_type = trigger.get("type", "")

    recovered = False
    if trigger_type == "btc_below_avg":
        recovered = _check_btc_recovery(db_conn)
    elif trigger_type:
        return True, reason

    if recovered:
        count = gates.get("recovery_count", 0) + 1
        do_clear = count >= gates.get("recovery_threshold", RECOVERY_THRESHOLD)
        gates["recovery_count"] = count
        if do_clear:
            _write_clear_gate()
            return False, f"auto-resumed after {count} recovery checks: {trigger_type}"
        _write_partial(gates)
        return True, f"{reason} (recovery {count}/{gates.get('recovery_threshold', RECOVERY_THRESHOLD)})"

    if gates.get("recovery_count", 0) > 0:
        gates["recovery_count"] = 0
        _write_partial(gates)
    return True, reason


def signal_architect_rethink(trigger_reason):
    """Write a flag file for the market-move watchdog."""
    try:
        with open(ARCHITECT_TRIGGER_FILE, "w") as f:
            f.write(json.dumps({
                "triggered_at": datetime.now(timezone.utc).isoformat(),
                "reason": trigger_reason,
            }))
    except IOError:
        pass


def _check_btc_recovery(db_conn):
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
        return False

    prices = [float(r[0]) for r in rows]
    current = prices[0]
    avg_6h = sum(prices) / len(prices)
    return current > avg_6h * BTC_RECOVERY_FACTOR


def _write_clear_gate():
    _atomic_write({
        "script_paused": False,
        "consult_on_entry": False,
        "reason": "Auto-resume: condition recovered",
        "paused_since": None,
        "trigger": None,
        "recovery_count": 0,
        "recovery_threshold": RECOVERY_THRESHOLD,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })


def _write_partial(gates):
    gates["updated_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_write(gates)


def _atomic_write(data):
    try:
        os.makedirs(os.path.dirname(AI_GATE_FILE), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(AI_GATE_FILE))
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, AI_GATE_FILE)
    except (IOError, OSError):
        pass