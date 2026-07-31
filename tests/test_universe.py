"""Universe defaults, normalization, and admin helpers (no live exchange)."""

from traders.common.universe import (
    DEFAULT_CRYPTO_PAIRS,
    DEFAULT_STOCK_SYMBOLS,
    get_crypto_pairs,
    get_stock_symbols,
    normalize_symbol,
)


def test_defaults_sizes():
    assert len(DEFAULT_STOCK_SYMBOLS) == 15
    assert "SPY" in DEFAULT_STOCK_SYMBOLS
    assert "JPM" in DEFAULT_STOCK_SYMBOLS
    assert len(DEFAULT_CRYPTO_PAIRS) == 12
    assert "BTC/EUR" in DEFAULT_CRYPTO_PAIRS


def test_get_symbols_fallback_without_db(monkeypatch):
    # Force DB miss by clearing env-like host
    monkeypatch.delenv("DB_HOST", raising=False)
    monkeypatch.delenv("DB_NAME", raising=False)
    from traders.common import universe as u
    u.invalidate_cache()
    assert get_stock_symbols() == list(DEFAULT_STOCK_SYMBOLS)
    assert get_crypto_pairs() == list(DEFAULT_CRYPTO_PAIRS)


def test_normalize_stock():
    assert normalize_symbol("stock", " nvda ") == "NVDA"


def test_normalize_crypto_always_eur():
    assert normalize_symbol("crypto", "sol") == "SOL/EUR"
    assert normalize_symbol("crypto", "BTC/USD") == "BTC/EUR"
    assert normalize_symbol("crypto", "eth/eur") == "ETH/EUR"


def test_strategy_configs_import_defaults():
    from traders.strategies.momentum import config as mo
    from traders.strategies.pullback import config as pb
    from traders.strategies.grid import config as gc

    assert mo.CRYPTO_PAIRS == DEFAULT_CRYPTO_PAIRS
    assert pb.CRYPTO_PAIRS == DEFAULT_CRYPTO_PAIRS
    assert gc.CRYPTO_PAIRS == DEFAULT_CRYPTO_PAIRS


def test_alpaca_module_uses_defaults_constant():
    import os
    import sys

    os.environ.setdefault("ALPACA_API_KEY", "test-key")
    os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret")
    os.environ.pop("AITRADER_MODE", None)
    mod_name = "traders.trades.alpaca_stocks"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    import traders.trades.alpaca_stocks as mod

    assert set(DEFAULT_STOCK_SYMBOLS).issubset(set(mod.STOCK_SYMBOLS))
