"""Focused Phase B safety regressions; all DB doubles are isolated in-memory fakes."""
import sys
import types
from pathlib import Path
from datetime import datetime, timedelta, timezone

from traders.extreme import db_prices
from traders.strategies.regime import detector, router
from traders.common.paper_wallet import paper_wallet_balance

# position_monitor imports optional live-exchange libraries at module load; no
# live client is created or called by these deterministic unit tests.
sys.modules.setdefault("ccxt", types.SimpleNamespace())
if "openai" not in sys.modules:
    sys.modules["openai"] = types.SimpleNamespace(OpenAI=object)
import position_monitor


class Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.result = None

    def __enter__(self): return self
    def __exit__(self, *args): return False

    def execute(self, sql, params=None):
        self.conn.calls.append((sql, params))
        if self.conn.fail:
            raise RuntimeError("database unavailable")
        if "SELECT timestamp, unrealized_plpc" in sql:
            self.result = self.conn.rows
        elif "SELECT 1 FROM trade_log" in sql:
            self.result = [(1,)] if params[1] in self.conn.order_ids else []

    def fetchall(self): return self.result or []
    def fetchone(self): return (self.result or [None])[0]
    def close(self): pass


class Conn:
    def __init__(self, rows=(), fail=False):
        self.rows, self.fail = list(rows), fail
        self.calls, self.order_ids = [], set()
        self.rollbacks = 0
    def cursor(self): return Cursor(self)
    def rollback(self): self.rollbacks += 1
    def commit(self): pass


def test_loss_streak_is_exact_strategy_decimal_and_time_bounded(monkeypatch):
    now = datetime.now(timezone.utc)
    conn = Conn([(now - timedelta(minutes=1), 0.0051)] * 3)
    assert db_prices.loss_streak_cooldown(conn, "kraken-momentum") == (True, db_prices.loss_streak_cooldown(conn, "kraken-momentum")[1])
    sql, params = conn.calls[0]
    assert "exchange=%s" in sql and "unrealized_plpc" in sql
    assert params == ("kraken-momentum", 24, 3)
    assert db_prices.ROUND_TRIP_FEE_DECIMAL == 0.0052
    paper = Conn([(now - timedelta(minutes=1), 0.0051)] * 3)
    assert db_prices.loss_streak_cooldown(paper, "paper-kraken-momentum")[0] is True
    assert paper.calls[0][1][0] == "paper-kraken-momentum"


def test_loss_streak_fewer_rows_winner_and_expiry_do_not_block():
    now = datetime.now(timezone.utc)
    assert db_prices.loss_streak_cooldown(Conn([(now, 0.0)] * 2), "paper-kraken-momentum")[0] is False
    assert db_prices.loss_streak_cooldown(Conn([(now, 0.0), (now, 0.006), (now, 0.0)]), "paper-kraken-momentum")[0] is False
    assert db_prices.loss_streak_cooldown(Conn([(now - timedelta(hours=6, seconds=1), 0.0)] * 3), "paper-kraken-momentum")[0] is False


def test_loss_streak_query_failure_rolls_back_and_fails_closed():
    conn = Conn(fail=True)
    blocked, _ = db_prices.loss_streak_cooldown(conn, "kraken-high-risk")
    assert blocked is True and conn.rollbacks == 1


def test_canonical_sell_uses_fill_decimal_quantity_and_is_idempotent(monkeypatch):
    conn = Conn()
    captured = []
    monkeypatch.setattr(db_prices, "log_trade", lambda *a, **kw: captured.append(kw) or True)
    order = {"id": "order-1", "average": 123.0, "filled": 0.25}
    assert db_prices.log_successful_sell_once(conn, "kraken-momentum", ticker="BTC/EUR", entry_price=100,
                                              price_hint=120, quantity=1, order=order, reason="rotation")
    assert captured == [{**captured[0], "current_price": 123.0, "quantity": 0.25,
                         "estimated_value": 30.75, "unrealized_plpc": 0.23}]
    conn.order_ids.add("order-1")
    assert db_prices.log_successful_sell_once(conn, "kraken-momentum", ticker="BTC/EUR", entry_price=100,
                                              price_hint=120, quantity=1, order=order, reason="rotation")
    assert len(captured) == 1


def test_monitor_closes_exact_state_after_telemetry_failure(monkeypatch):
    conn = Conn()
    monkeypatch.setattr(position_monitor, "log_successful_sell_once", lambda *a, **k: False)
    assert position_monitor._close_executed_sell(
        conn, "kraken-momentum", "BTC/EUR", entry_price=100, price_hint=90,
        quantity=1, order={"id": "one"}, reason="hard stop") is False
    deletes = [(sql, params) for sql, params in conn.calls if sql.startswith("DELETE FROM trading_state")]
    assert deletes == [("DELETE FROM trading_state WHERE exchange=%s AND symbol=%s", ("kraken-momentum", "BTC/EUR"))]


def test_monitor_hard_stop_and_llm_close_log_before_state_removal(monkeypatch):
    conn, events = Conn(), []
    monkeypatch.setattr(position_monitor, "log_successful_sell_once", lambda *a, **k: events.append(k["reason"]) or True)
    position_monitor._close_executed_sell(conn, "kraken-pullback", "ETH/EUR", entry_price=100,
                                          price_hint=85, quantity=2, order={"id": "hard"}, reason="Position monitor hard stop")
    position_monitor._close_executed_sell(conn, "kraken-pullback", "ETH/EUR", entry_price=100,
                                          price_hint=110, quantity=2, order={"id": "llm"}, reason="Position monitor LLM SELL: take profit")
    assert events == ["Position monitor hard stop", "Position monitor LLM SELL: take profit"]
    assert all(call[0].startswith("DELETE FROM trading_state") for call in conn.calls[-2:])


def test_monitor_second_cycle_cannot_duplicate_sell_after_telemetry_failure(monkeypatch):
    class Exchange:
        def __init__(self): self.sells = 0
        def amount_to_precision(self, symbol, quantity): return quantity
    conn, exchange = Conn(), Exchange()
    active = {"BTC/EUR"}
    monkeypatch.setattr(position_monitor, "market_sell", lambda *a: setattr(exchange, "sells", exchange.sells + 1) or {"id": "one"})
    monkeypatch.setattr(position_monitor, "log_successful_sell_once", lambda *a, **k: False)
    pos = {"symbol": "BTC/EUR", "state_exchange": "kraken-momentum", "entry": 100, "current": 80, "qty": 1}
    for _ in range(2):
        if pos["symbol"] in active:
            position_monitor._sell_position_once(conn, exchange, pos, "Position monitor hard stop")
            active.remove(pos["symbol"])
    assert exchange.sells == 1


def test_all_in_scope_strategy_sells_use_canonical_helper_and_partial_retains_state():
    root = Path(__file__).parents[1]
    for name, expected_sells in (("kraken_pullback.py", 1), ("kraken_momentum.py", 3), ("kraken_high_risk.py", 3)):
        source = (root / "traders" / "crypto_trades" / name).read_text()
        assert source.count("market_sell(") == expected_sells
        assert source.count("log_successful_sell_once(") == expected_sells
        assert 'log_trade(db_conn, action="SELL"' not in source
    momentum = (root / "traders" / "crypto_trades" / "kraken_momentum.py").read_text()
    ladder = momentum[momentum.index("# Laddered partial take-profit"):momentum.index("# DCA follow-up")]
    assert 'ss["quantity"] = max(0.0, qty - fqty)' in ladder and "new_state[symbol] = ss" in ladder
    assert "new_state.pop(symbol, None)" not in ladder


def test_kelly_callers_assign_zero_on_exception_before_any_market_buy():
    root = Path(__file__).parents[1]
    for name in ("kraken_pullback.py", "kraken_momentum.py", "kraken_high_risk.py"):
        source = (root / "traders" / "crypto_trades" / name).read_text()
        start = source.index("if USE_KELLY_SIZING") if name == "kraken_pullback.py" else source.index("# Kelly sizing")
        kelly = source[start:source.index("market_buy(", start)]
        assert "order_size_eur = 0.0" in kelly
        assert "if order_size_eur < MIN_TRADE_EUR:" in kelly


class RegimeConn(Conn):
    def __init__(self, rows=(), regime_row=None, fail=False):
        super().__init__(rows, fail)
        self.regime_row = regime_row

    def cursor(self):
        conn = self

        class RegimeCursor(Cursor):
            def execute(self, sql, params=None):
                conn.calls.append((sql, params))
                self.result = conn.rows if "FROM asset_prices" in sql else None

        return RegimeCursor(self)


def test_regime_uses_daily_closes_and_momentum_routing_fails_closed(monkeypatch):
    """A 20-day regime must not be inferred from 20 five-minute ticks."""
    # The SQL orders daily rows newest first, as PostgreSQL would.
    rows = list(reversed([(100 + day, datetime(2026, 1, day + 1, tzinfo=timezone.utc)) for day in range(21)]))
    conn = RegimeConn(rows)
    assert detector.detect_regime(conn, "BTC") == "trending"
    query = conn.calls[0][0]
    assert "DISTINCT ON" in query and "date_trunc('day'" in query

    class MissingRegime:
        def rollback(self): pass
        def cursor(self):
            class C:
                def execute(self, *_): pass
                def fetchone(self): return None
                def close(self): pass
            return C()

    allowed, reason = router.should_enter(MissingRegime(), "BTC", "momentum")
    assert allowed is False and "unavailable" in reason


def test_market_data_sanity_rejects_stale_or_divergent_cross_source_prices():
    now = datetime.now(timezone.utc)

    class MarketCursor(Cursor):
        def execute(self, sql, params=None):
            self.conn.calls.append((sql, params))
            self.result = self.conn.rows

    class MarketConn(Conn):
        def cursor(self): return MarketCursor(self)

    fresh = MarketConn([(100.0, now - timedelta(minutes=2))])
    assert db_prices.market_data_sane(fresh, "BTC/EUR", 100.4) == (True, "")
    stale = MarketConn([(100.0, now - timedelta(minutes=31))])
    assert db_prices.market_data_sane(stale, "BTC/EUR", 100.0)[0] is False
    divergent = MarketConn([(100.0, now - timedelta(minutes=2))])
    assert db_prices.market_data_sane(divergent, "BTC/EUR", 106.0)[0] is False


def test_market_data_sanity_rejects_invalid_observed_prices_without_raising():
    now = datetime.now(timezone.utc)

    class MarketCursor(Cursor):
        def execute(self, sql, params=None):
            self.conn.calls.append((sql, params))
            self.result = self.conn.rows

    class MarketConn(Conn):
        def cursor(self): return MarketCursor(self)

    for observed_price in (float("nan"), float("inf"), float("-inf"), "not-a-price", 0, -1):
        result = db_prices.market_data_sane(
            MarketConn([(100.0, now - timedelta(minutes=2))]), "BTC/EUR", observed_price)
        assert result[0] is False and result[1]


def test_market_data_sanity_rejects_invalid_persisted_prices_without_raising():
    now = datetime.now(timezone.utc)

    class MarketCursor(Cursor):
        def execute(self, sql, params=None):
            self.conn.calls.append((sql, params))
            self.result = self.conn.rows

    class MarketConn(Conn):
        def cursor(self): return MarketCursor(self)

    for db_price in (float("nan"), float("inf"), float("-inf"), "not-a-price", 0, -1):
        result = db_prices.market_data_sane(
            MarketConn([(db_price, now - timedelta(minutes=2))]), "BTC/EUR", 100.0)
        assert result[0] is False and result[1]


def test_paper_wallet_and_monitor_keys_are_completely_mode_isolated():
    assert position_monitor._STATE_EXCHANGES == (
        "kraken-momentum", "kraken-pullback", "kraken-high-risk", "kraken")
    # Re-import-independent contract for the paper key set.
    assert position_monitor.paper_state_exchanges() == (
        "paper-kraken-momentum", "paper-kraken-pullback", "paper-kraken-high-risk")
    root = Path(__file__).parents[1]
    for name in ("kraken_pullback.py", "kraken_momentum.py", "kraken_high_risk.py"):
        source = (root / "traders" / "crypto_trades" / name).read_text()
        assert "paper_wallet_balance" in source
        assert "paper_wallet_balance(state, tickers)" in source
        assert "if _PAPER_MODE:" in source  # paper never recovers a private fill
        assert "[] if _PAPER_MODE else exchange.fetch_open_orders()" in source
    for name in ("kraken_momentum.py", "kraken_high_risk.py"):
        assert "market_data_sane(" in (root / "traders" / "crypto_trades" / name).read_text()


def test_paper_wallet_is_derived_only_from_namespaced_simulation_state(monkeypatch):
    monkeypatch.setenv("PAPER_KRAKEN_STARTING_EUR", "100")
    wallet = paper_wallet_balance(
        {"BTC/EUR": {"quantity": 0.25, "total_position_eur": 40}},
        {"BTC/EUR": {"last": 200}},
    )
    assert wallet["free"]["EUR"] == 60
    assert wallet["total"]["BTC"] == 0.25


def test_regime_direction_overrides_volatility_and_uses_true_twenty_day_return():
    assert detector._classify_regime(adx=30, vol20=80, ret20=12) == "trending"
    assert detector._classify_regime(adx=30, vol20=80, ret20=-12) == "crisis"
    assert detector._classify_regime(adx=45, vol20=10, ret20=-1) == "uncertain"
    assert detector._return_pct(list(range(100, 121)), 20) == 20.0
    assert detector._return_pct(list(range(100, 120)), 20) is None

    # The production detector, not only the classifier, fails closed on low data.
    assert detector.detect_regime(RegimeConn([(100, datetime.now(timezone.utc))] * 20), "BTC") == "uncertain"


def test_missing_regime_bootstraps_once_and_failed_bootstrap_blocks_momentum(monkeypatch):
    class BootstrapConn:
        def __init__(self): self.row = None; self.calls = []
        def rollback(self): pass
        def cursor(self):
            outer = self
            class C:
                def execute(self, sql, params=None): outer.calls.append(sql)
                def fetchone(self): return outer.row
                def close(self): pass
            return C()

    conn = BootstrapConn()
    monkeypatch.setattr(router, "detect_regime", lambda db, sym: setattr(db, "row", ("trending",)))
    assert router.should_enter(conn, "BTC", "momentum")[0] is True
    assert len(conn.calls) >= 2

    failed = BootstrapConn()
    monkeypatch.setattr(router, "detect_regime", lambda *_: (_ for _ in ()).throw(RuntimeError("no db")))
    assert router.should_enter(failed, "BTC", "momentum") == (False, "regime unavailable (refresh failed)")


class _MomentumConn:
    """Cursor fake returning latest, target, then nearby target samples."""
    def __init__(self, latest, target, neighbours):
        self.responses = [latest, target, neighbours]
        self.rollbacks = 0
    def rollback(self): self.rollbacks += 1
    def cursor(self):
        outer = self
        class C:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def execute(self, *_): self.result = outer.responses.pop(0)
            def fetchone(self): return self.result
            def fetchall(self): return self.result
        return C()


def test_timestamp_aware_momentum_rejects_bad_history_and_accepts_coherent_breakout():
    now = datetime.now(timezone.utc)
    valid = _MomentumConn((110, now - timedelta(minutes=1)), (100, now - timedelta(minutes=60)),
                         [(100, now - timedelta(minutes=60)), (100.5, now - timedelta(minutes=59)), (99.8, now - timedelta(minutes=61))])
    assert db_prices.get_momentum_over(valid, "BTC/EUR", 60) == 10.0
    stale = _MomentumConn((110, now - timedelta(minutes=16)), (100, now - timedelta(minutes=60)), [])
    assert db_prices.get_momentum_over(stale, "BTC/EUR", 60) is None
    wrong_age = _MomentumConn((110, now - timedelta(minutes=1)), (100, now - timedelta(minutes=40)), [])
    assert db_prices.get_momentum_over(wrong_age, "BTC/EUR", 60) is None
    bad_values = _MomentumConn((float("nan"), now), (0, now - timedelta(minutes=60)), [])
    assert db_prices.get_momentum_over(bad_values, "BTC/EUR", 60) is None
    nonpositive_target = _MomentumConn((110, now), (0, now - timedelta(minutes=60)), [])
    assert db_prices.get_momentum_over(nonpositive_target, "BTC/EUR", 60) is None
    isolated = _MomentumConn((110, now), (1, now - timedelta(minutes=60)),
                             [(1, now - timedelta(minutes=60)), (100, now - timedelta(minutes=59)), (101, now - timedelta(minutes=61)), (99, now - timedelta(minutes=62))])
    assert db_prices.get_momentum_over(isolated, "BTC/EUR", 60) is None
    coherent = _MomentumConn((150, now), (125, now - timedelta(minutes=60)),
                             [(125, now - timedelta(minutes=60)), (124, now - timedelta(minutes=59)), (126, now - timedelta(minutes=61)), (125.5, now - timedelta(minutes=62))])
    assert db_prices.get_momentum_over(coherent, "BTC/EUR", 60) == 20.0


def test_paper_runtime_helpers_do_not_touch_private_kraken_methods(monkeypatch):
    """Runtime calls, not source inspection: paper branches must be inert privately."""
    import importlib
    class ExplosiveExchange:
        def fetch_balance(self): raise AssertionError("private balance")
        def fetch_my_trades(self, *_args, **_kwargs): raise AssertionError("private fills")
        def fetch_open_orders(self, *_args, **_kwargs): raise AssertionError("private orders")
        def amount_to_precision(self, _symbol, quantity): return quantity

    monkeypatch.setenv("AITRADER_MODE", "paper")
    monkeypatch.setenv("KRAKEN_API_KEY", "test")
    monkeypatch.setenv("KRAKEN_SECRET", "test")
    monkeypatch.setattr(sys.modules["ccxt"], "kraken", lambda *_args, **_kwargs: ExplosiveExchange(), raising=False)
    for path in ("traders.crypto_trades.kraken_pullback", "traders.crypto_trades.kraken_momentum",
                 "traders.crypto_trades.kraken_high_risk"):
        mod = importlib.reload(importlib.import_module(path))
        mod.exchange = ExplosiveExchange()
        entry, _ = mod.get_entry_price_and_time("BTC/EUR", 100)
        assert entry == 100
    for path in ("traders.crypto_trades.kraken_momentum", "traders.crypto_trades.kraken_high_risk"):
        mod = importlib.import_module(path)
        assert mod.sellable_qty("BTC/EUR", 0.25) == 0.25


def test_paper_monitor_state_scope_rejects_live_or_bare_keys(monkeypatch):
    monkeypatch.setattr(position_monitor, "_PAPER_MODE", True)
    assert position_monitor.monitored_state_exchanges() == position_monitor.paper_state_exchanges()
    conn = Conn()
    with __import__("pytest").raises(ValueError):
        position_monitor._close_executed_sell(conn, "kraken", "BTC/EUR", entry_price=1,
                                              price_hint=1, quantity=1, order={"id": "x"}, reason="test")
    monkeypatch.setattr(position_monitor, "_PAPER_MODE", False)
    assert position_monitor.monitored_state_exchanges() == position_monitor._LIVE_STATE_EXCHANGES
    monkeypatch.setattr(position_monitor, "log_successful_sell_once", lambda *a, **k: True)
    position_monitor._close_executed_sell(conn, "kraken", "BTC/EUR", entry_price=1,
                                          price_hint=1, quantity=1, order={"id": "x"}, reason="test")
    assert conn.calls[-1][1][0] == "kraken"
