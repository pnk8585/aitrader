"""Focused deterministic contracts for the research backtest engine."""

from datetime import datetime, timedelta, timezone

import pytest

from scripts import backtest_strategy as backtest


def test_momentum_engine_completes_a_synthetic_stop_loss_trade(monkeypatch):
    start = datetime(2026, 8, 18, tzinfo=timezone.utc)
    prices = {"BTC/EUR": [(start, 100.0), (start + timedelta(minutes=5), 97.0)]}

    monkeypatch.setattr(backtest, "load_prices", lambda *args, **kwargs: prices)
    monkeypatch.setattr(backtest, "compute_atr_from_prices", lambda *args, **kwargs: 1.0)
    signals = iter([{"signal": "MODERATE_MOMENTUM", "mult": 1.0}, None])
    monkeypatch.setattr(backtest, "check_momentum_entry", lambda *args, **kwargs: next(signals))

    metrics = backtest.backtest_engine(
        db_conn=None,
        strategies=["momentum"],
        initial_balance=100.0,
        use_atr_stops=False,
        use_kelly_sizing=False,
    )

    assert metrics["total_trades"] == 1
    assert metrics["closed_trades"][0]["reason"].startswith("Stop-loss")


def test_pullback_engine_completes_a_synthetic_exit_without_stale_exit_kwargs(monkeypatch):
    start = datetime(2026, 8, 18, tzinfo=timezone.utc)
    prices = {"BTC/EUR": [(start, 100.0), (start + timedelta(minutes=5), 97.0)]}

    monkeypatch.setattr(backtest, "load_prices", lambda *args, **kwargs: prices)
    monkeypatch.setattr(backtest, "compute_atr_from_prices", lambda *args, **kwargs: 1.0)
    signals = iter([{"signal": "PULLBACK", "mult": 1.0}, None])
    monkeypatch.setattr(backtest, "check_pullback_entry", lambda *args, **kwargs: next(signals))

    metrics = backtest.backtest_engine(
        db_conn=None,
        strategies=["pullback"],
        initial_balance=100.0,
        use_atr_stops=False,
        use_kelly_sizing=False,
    )

    assert metrics["total_trades"] == 1
    assert metrics["closed_trades"][0]["reason"].startswith("Hard stop")


def test_exit_returns_principal_and_gain_to_available_cash(monkeypatch):
    start = datetime(2026, 8, 18, tzinfo=timezone.utc)
    prices = {"BTC/EUR": [(start, 100.0), (start + timedelta(minutes=5), 110.0)]}
    monkeypatch.setattr(backtest, "load_prices", lambda *args, **kwargs: prices)
    monkeypatch.setattr(backtest, "compute_atr_from_prices", lambda *args, **kwargs: 1.0)
    monkeypatch.setattr(backtest, "should_exit_momentum", lambda **kwargs: (kwargs["age_hours"] > 0, "test exit"))
    signals = iter([{"signal": "MODERATE_MOMENTUM", "mult": 1.0}, None])
    monkeypatch.setattr(backtest, "check_momentum_entry", lambda *args, **kwargs: next(signals))

    metrics = backtest.backtest_engine(
        db_conn=None, strategies=["momentum"], initial_balance=100.0,
        use_atr_stops=False, use_kelly_sizing=False, entry_fee_pct=0.0, exit_fee_pct=0.0,
    )

    assert metrics["final_cash"] == 101.0
    assert metrics["final_equity"] == 101.0
    assert metrics["closed_trades"][0]["gross_pnl"] == 1.0


def test_flat_round_trip_is_a_net_loser_by_configured_fees(monkeypatch):
    start = datetime(2026, 8, 18, tzinfo=timezone.utc)
    prices = {"BTC/EUR": [(start, 100.0), (start + timedelta(minutes=5), 100.0)]}
    monkeypatch.setattr(backtest, "load_prices", lambda *args, **kwargs: prices)
    monkeypatch.setattr(backtest, "compute_atr_from_prices", lambda *args, **kwargs: 1.0)
    monkeypatch.setattr(backtest, "should_exit_momentum", lambda **kwargs: (kwargs["age_hours"] > 0, "test exit"))
    signals = iter([{"signal": "MODERATE_MOMENTUM", "mult": 1.0}, None])
    monkeypatch.setattr(backtest, "check_momentum_entry", lambda *args, **kwargs: next(signals))

    metrics = backtest.backtest_engine(
        db_conn=None, strategies=["momentum"], initial_balance=100.0,
        use_atr_stops=False, use_kelly_sizing=False,
        entry_fee_pct=0.26, exit_fee_pct=0.26, slippage_pct=0.0,
    )

    trade = metrics["closed_trades"][0]
    assert trade["gross_pnl"] == 0.0
    assert trade["net_pnl"] == pytest.approx(-0.052, abs=1e-9)
    assert metrics["win_count"] == 0


def test_net_percentage_metrics_treat_a_gross_winner_as_a_net_loser(monkeypatch):
    start = datetime(2026, 8, 18, tzinfo=timezone.utc)
    prices = {"BTC/EUR": [(start, 100.0), (start + timedelta(minutes=5), 100.2)]}
    monkeypatch.setattr(backtest, "load_prices", lambda *args, **kwargs: prices)
    monkeypatch.setattr(backtest, "compute_atr_from_prices", lambda *args, **kwargs: 1.0)
    monkeypatch.setattr(backtest, "should_exit_momentum", lambda **kwargs: (kwargs["age_hours"] > 0, "test exit"))
    signals = iter([{"signal": "MODERATE_MOMENTUM", "mult": 1.0}, None])
    monkeypatch.setattr(backtest, "check_momentum_entry", lambda *args, **kwargs: next(signals))

    metrics = backtest.backtest_engine(
        None, strategies=["momentum"], initial_balance=100.0,
        use_atr_stops=False, use_kelly_sizing=False,
        entry_fee_pct=0.26, exit_fee_pct=0.26, slippage_pct=0.0,
    )

    trade = metrics["closed_trades"][0]
    assert trade["gross_plpc"] > 0
    assert trade["net_plpc"] < 0
    assert metrics["win_count"] == 0
    assert metrics["avg_loss_plpc"] == pytest.approx(trade["net_plpc"], abs=0.01)


def test_negative_cost_inputs_are_rejected(monkeypatch):
    monkeypatch.setattr(backtest, "load_prices", lambda *args, **kwargs: {})

    with pytest.raises(ValueError, match="non-negative"):
        backtest.backtest_engine(None, strategies=["momentum"], slippage_pct=-0.01)


def test_open_position_equity_is_marked_and_force_close_credits_once(monkeypatch):
    start = datetime(2026, 8, 18, tzinfo=timezone.utc)
    prices = {"BTC/EUR": [(start, 100.0), (start + timedelta(minutes=5), 110.0)]}
    monkeypatch.setattr(backtest, "load_prices", lambda *args, **kwargs: prices)
    monkeypatch.setattr(backtest, "compute_atr_from_prices", lambda *args, **kwargs: 1.0)
    monkeypatch.setattr(backtest, "should_exit_momentum", lambda **kwargs: (False, ""))
    signals = iter([{"signal": "MODERATE_MOMENTUM", "mult": 1.0}, None])
    monkeypatch.setattr(backtest, "check_momentum_entry", lambda *args, **kwargs: next(signals))

    metrics = backtest.backtest_engine(
        None, strategies=["momentum"], initial_balance=100.0,
        use_atr_stops=False, use_kelly_sizing=False, entry_fee_pct=0.0, exit_fee_pct=0.0,
    )

    assert metrics["equity_curve"][1] == 101.0
    assert metrics["final_cash"] == 101.0
    assert metrics["closed_trades"][-1]["reason"] == "force-close (end of data)"


def test_price_cache_never_uses_a_future_price():
    start = datetime(2026, 8, 18, tzinfo=timezone.utc)
    cache = backtest.PriceCache({
        "BTC/EUR": [(start, 100.0), (start + timedelta(minutes=10), 120.0)],
    })

    assert cache.price_at("BTC/EUR", start + timedelta(minutes=6)) == 100.0


def test_canonical_cycles_coalesce_asynchronous_symbol_timestamps():
    start = datetime(2026, 8, 18, 12, 0, 30, tzinfo=timezone.utc)
    price_data = {
        "BTC/EUR": [(start, 100.0)],
        "ETH/EUR": [(start + timedelta(seconds=45), 100.0)],
    }

    assert backtest.canonical_cycle_timestamps(price_data) == [
        datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc),
    ]


def _momentum_signal(symbol, price, score=3.0):
    return {
        "strategy": "momentum", "symbol": symbol, "price": price,
        "signal": "MODERATE_MOMENTUM", "mult": 1.0,
        "daily": score, "hourly": score,
    }


def test_momentum_cycle_selects_only_the_best_candidate(monkeypatch):
    start = datetime(2026, 8, 18, tzinfo=timezone.utc)
    prices = {
        "BTC/EUR": [(start, 100.0)],
        "ETH/EUR": [(start, 100.0)],
    }
    monkeypatch.setattr(backtest, "load_prices", lambda *args, **kwargs: prices)
    monkeypatch.setattr(backtest, "compute_atr_from_prices", lambda *args, **kwargs: 1.0)
    monkeypatch.setattr(backtest, "check_momentum_entry", lambda _c, symbol, _t, price, cfg=None:
                        _momentum_signal(symbol, price, 4.0 if symbol == "ETH/EUR" else 3.0))

    metrics = backtest.backtest_engine(None, strategies=["momentum"], initial_balance=100.0,
                                       use_atr_stops=False, use_kelly_sizing=False)

    assert [entry["symbol"] for entry in metrics["momentum_entries"]] == ["ETH/EUR"]


def test_momentum_position_cap_blocks_additional_entries(monkeypatch):
    start = datetime(2026, 8, 18, tzinfo=timezone.utc)
    prices = {
        "BTC/EUR": [(start, 100.0), (start + timedelta(minutes=5), 100.0)],
        "ETH/EUR": [(start + timedelta(minutes=5), 100.0)],
    }
    monkeypatch.setattr(backtest, "load_prices", lambda *args, **kwargs: prices)
    monkeypatch.setattr(backtest, "compute_atr_from_prices", lambda *args, **kwargs: 1.0)
    monkeypatch.setattr(backtest, "should_exit_momentum", lambda **kwargs: (False, ""))
    monkeypatch.setattr(backtest, "check_momentum_entry", lambda _c, symbol, _t, price, cfg=None:
                        _momentum_signal(symbol, price))

    metrics = backtest.backtest_engine(None, strategies=["momentum"], initial_balance=100.0,
                                       use_atr_stops=False, use_kelly_sizing=False,
                                       momentum_max_open=1)

    assert len(metrics["momentum_entries"]) == 1


def test_momentum_daily_trade_cap_blocks_third_entry(monkeypatch):
    start = datetime(2026, 8, 18, tzinfo=timezone.utc)
    prices = {"BTC/EUR": [(start + timedelta(minutes=5 * i), 100.0) for i in range(4)]}
    monkeypatch.setattr(backtest, "load_prices", lambda *args, **kwargs: prices)
    monkeypatch.setattr(backtest, "compute_atr_from_prices", lambda *args, **kwargs: 1.0)
    monkeypatch.setattr(backtest, "should_exit_momentum", lambda **kwargs: (kwargs["age_hours"] > 0, "test exit"))
    monkeypatch.setattr(backtest, "check_momentum_entry", lambda _c, symbol, _t, price, cfg=None:
                        _momentum_signal(symbol, price))

    metrics = backtest.backtest_engine(None, strategies=["momentum"], initial_balance=100.0,
                                       use_atr_stops=False, use_kelly_sizing=False,
                                       momentum_cooldown_min=0, momentum_max_trades_per_day=2)

    assert len(metrics["momentum_entries"]) == 2


def test_momentum_symbol_cooldown_blocks_reentry(monkeypatch):
    start = datetime(2026, 8, 18, tzinfo=timezone.utc)
    prices = {"BTC/EUR": [(start + timedelta(minutes=5 * i), 100.0) for i in range(3)]}
    monkeypatch.setattr(backtest, "load_prices", lambda *args, **kwargs: prices)
    monkeypatch.setattr(backtest, "compute_atr_from_prices", lambda *args, **kwargs: 1.0)
    monkeypatch.setattr(backtest, "should_exit_momentum", lambda **kwargs: (kwargs["age_hours"] > 0, "test exit"))
    monkeypatch.setattr(backtest, "check_momentum_entry", lambda _c, symbol, _t, price, cfg=None:
                        _momentum_signal(symbol, price))

    metrics = backtest.backtest_engine(None, strategies=["momentum"], initial_balance=100.0,
                                       use_atr_stops=False, use_kelly_sizing=False,
                                       momentum_cooldown_min=90)

    assert len(metrics["momentum_entries"]) == 1


def test_momentum_daily_loss_breaker_uses_closed_net_results(monkeypatch):
    start = datetime(2026, 8, 18, tzinfo=timezone.utc)
    prices = {
        # The -3.8% gross move crosses the -4pp breaker only after 0.26%
        # entry and exit fees are included in the closed net result.
        "BTC/EUR": [(start, 100.0), (start + timedelta(minutes=5), 96.2)],
        "ETH/EUR": [(start + timedelta(minutes=5), 100.0), (start + timedelta(minutes=10), 100.0)],
    }
    monkeypatch.setattr(backtest, "load_prices", lambda *args, **kwargs: prices)
    monkeypatch.setattr(backtest, "compute_atr_from_prices", lambda *args, **kwargs: 1.0)
    monkeypatch.setattr(backtest, "should_exit_momentum", lambda **kwargs: (kwargs["age_hours"] > 0, "test exit"))
    monkeypatch.setattr(backtest, "check_momentum_entry", lambda _c, symbol, _t, price, cfg=None:
                        _momentum_signal(symbol, price))

    metrics = backtest.backtest_engine(None, strategies=["momentum"], initial_balance=100.0,
                                       use_atr_stops=False, use_kelly_sizing=False,
                                       momentum_cooldown_min=0, momentum_daily_loss_breaker_pct=-4.0,
                                       entry_fee_pct=0.26, exit_fee_pct=0.26)

    assert [entry["symbol"] for entry in metrics["momentum_entries"]] == ["BTC/EUR"]
