"""AI gate utilities with condition-based auto-resume."""

import json
import os
import tempfile
from datetime import datetime, timezone

from traders.common.config import AI_GATE_FILE, ARCHITECT_TRIGGER_FILE

BTC_RECOVERY_FACTOR = 0.99
# Pause mirror of the recovery factor: pause the buyers when BTC trades >3% below
# its 6h average. Kept intentionally below BTC_RECOVERY_FACTOR (0.99) so the
# pause/recovery band has hysteresis and cannot flap between the two on a flat tape.
BTC_PAUSE_FACTOR = 0.97
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


def check_gate(db_conn):
    """Return (paused, reason_or_msg). Auto-clears stale pauses when recovered."""
    # Re-arm the BTC-drawdown pause on the buyer's own cadence (~5 min). The setter
    # is idempotent + None-safe, so calling it here every cycle is cheap and will not
    # reset check_gate's recovery bookkeeping (it short-circuits when already paused).
    # This restores the ~5-min re-arm cadence of the deleted ai_overseer.py; the 2h
    # position_monitor call remains as defense-in-depth if a buyer is down.
    check_and_set_btc_pause(db_conn)
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


def check_and_set_btc_pause(db_conn):
    """Pause the buyer scripts when BTC drops >3% below its 6h average.

    Mirror of the ``_check_btc_recovery`` auto-resume machinery: this is the
    *setter* half that the deleted ai_overseer used to own. Returns True when the
    scripts are (now or already) paused, else False.

    Idempotent: if a pause is already active we return True *without* rewriting the
    gate file — recovery bookkeeping (recovery_count/threshold) is owned solely by
    ``check_gate``, so re-writing here would reset its progress every cycle.
    The gate we write carries ``trigger={"type": "btc_below_avg"}`` so the existing
    ``check_gate`` auto-resume path clears it on recovery with zero extra code.
    """
    if load_ai_gates().get("script_paused"):
        return True

    stats = _btc_stats(db_conn)
    if stats is None:
        return False

    current, avg_6h = stats
    if current < avg_6h * BTC_PAUSE_FACTOR:
        pct = (current / avg_6h - 1.0) * 100.0
        _write_pause_gate(
            reason=f"BTC {pct:.1f}% below 6h avg (paused at {current:.0f})",
            trigger={"type": "btc_below_avg"},
        )
        return True
    return False


def _btc_stats(db_conn):
    """Return (current_price, 6h_avg) from the same window _check_btc_recovery reads,
    or None when the DB is unavailable or the sample is too small to trust."""
    if db_conn is None:
        return None
    try:
        cur = db_conn.cursor()
        cur.execute(
            "SELECT price FROM asset_prices "
            "WHERE exchange='kraken' AND symbol='BTC' "
            "ORDER BY timestamp DESC LIMIT 72")
        rows = cur.fetchall()
    except Exception:
        return None

    if not rows or len(rows) < 12:
        return None

    prices = [float(r[0]) for r in rows]
    return prices[0], sum(prices) / len(prices)


def _check_btc_recovery(db_conn):
    stats = _btc_stats(db_conn)
    if stats is None:
        return False
    current, avg_6h = stats
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


def _write_pause_gate(reason, trigger):
    """Write a fresh pause gate in the exact shape check_gate expects to read.

    Mirror of _write_clear_gate: same keys, inverted state. recovery_count starts at
    0 so check_gate's auto-resume counts from a clean slate on the next recovery."""
    _atomic_write({
        "script_paused": True,
        "consult_on_entry": False,
        "reason": reason,
        "paused_since": datetime.now(timezone.utc).isoformat(),
        "trigger": trigger,
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