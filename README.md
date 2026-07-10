# AITrader

Multi-strategy automated trading system for Kraken crypto and Alpaca US stocks, with an AI overseer layer for risk gating and daily regime guidance.

## Quick start

```bash
cp .env.example .env   # fill in credentials
pip install -e ".[dev]"
make test
```

## Trading bots

| Script | Exchange | Schedule (typical) |
|--------|----------|-------------------|
| `traders/crypto_trades/kraken_pullback.py` | Kraken | Every 5 min |
| `traders/crypto_trades/kraken_momentum.py` | Kraken | Every 5 min |
| `traders/trades/alpaca_stocks.py` | Alpaca | Every 5 min (market hours) |
| `traders/extreme/ai_overseer.py` | Kraken + AI | Hourly |

Both Kraken bots share one EUR wallet. Each uses its own lock file and `trading_state` namespace.

## Safety modes

| Env var | Effect |
|---------|--------|
| `DRY_RUN=true` | Log orders, do not submit to exchanges |
| `ALPACA_PAPER=true` | Route Alpaca API to paper trading |
| `DEBUG=true` | Propagate exceptions instead of swallowing |

## Operations

```bash
make health          # DB + exchange heartbeat check
make pnl             # P&L attribution from trade_log
python3 traders/extreme/system_health_check.py
```

### Cron example

```cron
*/5 * * * * cd /path/to/aitrader && python3 traders/crypto_trades/kraken_pullback.py
*/5 * * * * cd /path/to/aitrader && python3 traders/crypto_trades/kraken_momentum.py
*/5 * * * * cd /path/to/aitrader && python3 traders/trades/alpaca_stocks.py
0 * * * *   cd /path/to/aitrader && python3 traders/extreme/ai_overseer.py
```

### Kill switch

Pause all entries by writing `ai_overseer/ai_gate.json`:

```json
{"script_paused": true, "reason": "manual halt", "consult_on_entry": false}
```

Exits still run. Resume by setting `script_paused` to `false` or letting auto-resume clear a BTC-recovery pause.

## Project layout

```
traders/
  common/           # Shared gates, locks, strategy loader, exchange helpers
  strategies/     # Config, signals, exits per strategy
  crypto_trades/  # Kraken bot runners
  trades/         # Alpaca bot runner
  extreme/        # AI overseer, db_prices, health check
research/archive/ # Historical backtest scratch files
tests/            # pytest suite
```

## Research

```bash
make backtest      # Pullback filter simulation with fee/slippage model
```

See `.ai/` for strategy postmortems and design notes. `HERMES.md` is the agent entry point.