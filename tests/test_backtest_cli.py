"""Executable CLI contracts for the research backtest."""

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_backtest_help_runs_from_repository_root():
    result = subprocess.run(
        [sys.executable, "scripts/backtest_strategy.py", "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Backtest trading strategies" in result.stdout
