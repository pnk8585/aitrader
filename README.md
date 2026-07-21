# AITrader

Multi-strategy automated trading system — **Kraken crypto** + **Alpaca US stocks**, with LLM-evaluated entries and exits.

## Architecture

```
aitrader_orchestrator.py         ← cron every 1m, reads JSON registry
├─ kraken_pullback.py            LIVE   every 5m   ← pullback entries
├─ kraken_momentum.py            PAPER  every 5m   ← momentum entries
├─ position_monitor.py           LIVE   every 2h   ← sells stale positions
└─ alpaca_stocks.py              LIVE   every 5m   ← stock momentum
```

Every buy/sell is evaluated by a local LLM (`traders/common/llm_review.py`) that gets:
- **Price context**: 1h/6h/24h change, BTC price, 24h range (from `asset_prices` DB)
- **News headlines**: 3 latest headlines from DuckDuckGo (4s timeout)
- **Signal scores**: strategy-specific scoring from candidate analysis

All decisions logged to `llm_review_log` table for retrospective accuracy analysis.

## Quick start

```bash
cd /home/pank/projects/aitrader
source .venv/bin/activate

# Check status
python3 -c "import json; d=json.load(open('aitrader_orchestrator.json')); [print(f'{k:20s} {v[\"mode\"]:6s} {v[\"status\"]}') for k,v in d['scripts'].items()]"

# Run orchestrator manually
python3 aitrader_orchestrator.py

# Test LLM review
python3 traders/common/llm_review.py --symbol AVAX/EUR --strategy pullback --score 4.5 --price 5.82
```

## Script modes

| Mode | Orders | DB logging |
|------|--------|-----------|
| `live` | Real exchange API | Real trades |
| `paper` | Simulated fills | `paper-` prefix |
| `paused` | Skipped entirely | — |

Edit `aitrader_orchestrator.json` to change modes.

## Database tables

| Table | Purpose |
|-------|---------|
| `trade_log` | Every executed trade |
| `llm_review_log` | Every LLM evaluation (APPROVE/REJECT/SELL/HOLD) |
| `trading_state` | Open position tracking |
| `asset_prices` | 5-min price snapshots (Kraken) |

## Project layout

```
aitrader/
├── aitrader_orchestrator.py     # Main daemon
├── aitrader_orchestrator.json   # Script registry
├── aitrader_registry.py         # Atomic JSON state
├── position_monitor.py          # Position sell/hold monitor
│
├── traders/
│   ├── common/
│   │   ├── llm_review.py        # Sync LLM evaluation
│   │   ├── exchange.py          # Order routing (live/paper)
│   │   ├── gates.py             # Safety gates (BTC drawdown)

│   ├── crypto_trades/
│   │   ├── kraken_pullback.py   # Pullback entries
│   │   └── kraken_momentum.py   # Momentum entries
│   ├── trades/
│   │   └── alpaca_stocks.py     # Stock momentum
│   ├── strategies/
│   │   ├── pullback/            # Config, signals, exits
│   │   └── momentum/            # Config, exits
│   └── extreme/
│       └── db_prices.py         # Price data queries
│
└── tests/
```

## Operations

```bash
# View recent LLM rejections
python3 -c "
import os; from dotenv import load_dotenv; load_dotenv()
import psycopg2
conn = psycopg2.connect(host=os.environ['DB_HOST'], port=int(os.environ['DB_PORT']),
    dbname=os.environ['DB_NAME'], user=os.environ['DB_USER'], password=os.environ['DB_PASSWORD'])
cur = conn.cursor()
cur.execute(\"\"\"SELECT created_at, strategy, symbol, verdict, reason FROM llm_review_log WHERE verdict='REJECT' ORDER BY created_at DESC LIMIT 10\"\"\")
for r in cur.fetchall(): print(f\"{r[0].strftime('%m-%d %H:%M')} {r[1]:15s} {r[2]:10s} {r[3]:8s} | {r[4][:80]}\")
"
```

## Kill switch

Pause all entries by creating `ai_overseer/ai_gate.json`:
```json
{"script_paused": true, "reason": "manual halt"}
```
Exits still run. Resume by deleting the file or setting `script_paused: false`.
