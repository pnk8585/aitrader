"""Tests for AI gate auto-resume logic."""

import json
import os
import tempfile

from traders.common import gates


def test_load_ai_gates_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(gates, "AI_GATE_FILE", str(tmp_path / "missing.json"))
    g = gates.load_ai_gates()
    assert g["script_paused"] is False
    assert g["consult_on_entry"] is False


def test_check_gate_open_when_not_paused(tmp_path, monkeypatch):
    gate_file = tmp_path / "ai_gate.json"
    gate_file.write_text(json.dumps({"script_paused": False}))
    monkeypatch.setattr(gates, "AI_GATE_FILE", str(gate_file))
    paused, msg = gates.check_gate(None)
    assert paused is False
    assert msg is None


def test_check_gate_paused_without_trigger(tmp_path, monkeypatch):
    gate_file = tmp_path / "ai_gate.json"
    gate_file.write_text(json.dumps({
        "script_paused": True,
        "reason": "manual pause",
        "trigger": {"type": "unknown_type"},
    }))
    monkeypatch.setattr(gates, "AI_GATE_FILE", str(gate_file))
    paused, msg = gates.check_gate(None)
    assert paused is True
    assert "manual pause" in msg


def test_btc_recovery_auto_clear(tmp_path, monkeypatch):
    gate_file = tmp_path / "ai_gate.json"
    gate_file.write_text(json.dumps({
        "script_paused": True,
        "reason": "btc weak",
        "trigger": {"type": "btc_below_avg"},
        "recovery_count": 2,
        "recovery_threshold": 3,
    }))
    monkeypatch.setattr(gates, "AI_GATE_FILE", str(gate_file))

    class FakeCursor:
        def execute(self, *a, **k):
            pass

        def fetchall(self):
            return [(65000,)] * 20

    class FakeConn:
        def cursor(self):
            return FakeCursor()

    paused, msg = gates.check_gate(FakeConn())
    assert paused is False
    assert "auto-resumed" in msg
    cleared = json.loads(gate_file.read_text())
    assert cleared["script_paused"] is False