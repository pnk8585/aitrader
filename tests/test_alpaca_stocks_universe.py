"""Alpaca stocks universe + paper buy path regressions."""
import ast
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ALPACA_SRC = ROOT / "traders" / "trades" / "alpaca_stocks.py"
ORCH_SRC = ROOT / "app" / "cron_orchestrator.py"

EXPECTED_CORE = {
    "NVDA", "PLTR", "TSLA", "AMD", "GOOGL", "META", "AAPL", "MSFT", "AMZN", "AVGO",
}
EXPECTED_DIVERSIFIERS = {"JPM", "XOM", "UNH", "CAT", "SPY"}


def _import_alpaca_module():
    """Import trader after stubbing required env (module exits if keys missing)."""
    os.environ.setdefault("ALPACA_API_KEY", "test-key")
    os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret")
    # Avoid paper/live side effects from ambient env during unit tests.
    os.environ.pop("AITRADER_MODE", None)
    mod_name = "traders.trades.alpaca_stocks"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    import traders.trades.alpaca_stocks as mod  # noqa: WPS433
    return mod


def test_stock_symbols_has_core_and_diversifiers():
    mod = _import_alpaca_module()
    symbols = set(mod.STOCK_SYMBOLS)
    assert EXPECTED_CORE.issubset(symbols)
    assert EXPECTED_DIVERSIFIERS.issubset(symbols)
    assert len(mod.STOCK_SYMBOLS) == 15
    # No accidental duplicates
    assert len(mod.STOCK_SYMBOLS) == len(symbols)


def test_scanned_assets_slice_tracks_universe_size():
    src = ALPACA_SRC.read_text()
    assert "get_stock_symbols" in src
    assert "stock_symbols" in src


def test_paper_buy_path_does_not_call_order_res_json():
    """Regression: paper/dry-run success path used to NameError on unbound order_res."""
    src = ALPACA_SRC.read_text()
    # Isolate the BUY order block after order_data assignment.
    m = re.search(
        r'order_data = \{.*?\n\s*\}.*?if order_res_status in \[200, 201\]',
        src,
        re.DOTALL,
    )
    assert m, "BUY order block not found"
    block = m.group(0)
    # Success branch must not re-parse order_res.json() (unbound on paper).
    success_tail = src[m.end() : m.end() + 80]
    assert "order_res.json()" not in success_tail
    assert "order is not None" in src or "order is not None" in block + success_tail
    assert "order_err_text" in src


def test_cron_registry_alpaca_default_paper():
    src = ORCH_SRC.read_text()
    m = re.search(
        r'"alpaca-stocks"\s*:\s*\(\s*"[^"]+"\s*,\s*\d+\s*,\s*"(live|paper)"\s*\)',
        src,
    )
    assert m, "alpaca-stocks JOB_REGISTRY entry not found"
    assert m.group(1) == "paper"


def test_paper_exchange_name_prefix():
    os.environ["ALPACA_API_KEY"] = "test-key"
    os.environ["ALPACA_SECRET_KEY"] = "test-secret"
    os.environ["AITRADER_MODE"] = "paper"
    mod_name = "traders.trades.alpaca_stocks"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    import traders.trades.alpaca_stocks as mod  # noqa: WPS433
    assert mod.EXCHANGE_NAME == "paper-alpaca-stocks"
    assert mod._ALPACA_PAPER is True
