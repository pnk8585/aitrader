"""Tests for momentum exit logic."""

from traders.strategies.momentum.exits import is_stale_rotation_candidate, should_exit_momentum


def test_stop_loss():
    sell, reason = should_exit_momentum(unrealized_plpc=-3.0, peak_plpc=1.0, age_hours=1.0)
    assert sell is True
    assert "Stop-loss" in reason


def test_stale_rotation_flat():
    assert is_stale_rotation_candidate(unrealized_plpc=0.5, age_hours=1.0) is True


def test_not_stale_when_profitable():
    assert is_stale_rotation_candidate(unrealized_plpc=2.0, age_hours=1.0) is False


from types import SimpleNamespace

STOCK_CFG = SimpleNamespace(
    USE_ATR_STOPS=False,
    TTP_PEAK_PCT=3.0, TTP_GIVEBACK_PCT=1.0,
    PLOCK_PEAK_PCT=5.0, PLOCK_FLOOR_PCT=3.0,
    STOP_LOSS_PCT=-2.0, BREAKEVEN_PEAK_PCT=1.0,
    ROUND_TRIP_FEE_PCT=0.01, MAX_HOLD_HOURS=8.0,
)


def test_stock_cfg_trailing_tp():
    # Stock trail arms at +3% peak with 1% giveback (crypto arms at +2%/0.7%)
    sell, reason = should_exit_momentum(
        unrealized_plpc=1.9, peak_plpc=3.1, age_hours=1.0, cfg=STOCK_CFG,
    )
    assert sell is True
    assert "Trailing TP" in reason


def test_stock_cfg_holds_where_crypto_would_sell():
    # Peak +2.5% / now +1.5%: crypto trail (2.0/0.7) sells, stock trail (3.0/1.0) holds
    sell_crypto, _ = should_exit_momentum(
        unrealized_plpc=1.5, peak_plpc=2.5, age_hours=0.5,
    )
    sell_stock, _ = should_exit_momentum(
        unrealized_plpc=1.5, peak_plpc=2.5, age_hours=0.5, cfg=STOCK_CFG,
    )
    assert sell_crypto is True
    assert sell_stock is False
