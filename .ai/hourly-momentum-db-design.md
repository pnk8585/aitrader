# Hourly Momentum — Shared Price DB Design

## Directive (single-writer, shared prices)
1. **Kraken = only writer.** Monitors 12 coins (BTC/EUR, ETH/EUR, SOL/EUR, AVAX/EUR, LINK/EUR, XRP/EUR, DOGE/EUR, SUI/EUR, NEAR/EUR, RENDER/EUR, ADA/EUR, DOT/EUR) — superset of Alpaca's 5.
2. **Alpaca = read-only.** Queries Kraken's stored prices for hourly momentum. Never writes.
3. **DB symbol = base coin** (`BTC`, `ETH`), not full pair → trivial sharing, no USD/EUR mismatch.

## Coin universes
- Kraken `execute_kraken_cycle.py:28` → 12 EUR pairs.
- Alpaca `execute_cycle.py:25` → BTC/USD, ETH/USD, SOL/USD, AVAX/USD, LINK/USD (all ⊂ Kraken set).

## Key design decisions
- `base_symbol(pair) = pair.split("/")[0].upper()` normalizes both EUR and USD pairs.
- **Momentum computed from two Kraken-stored rows** (latest + ~1h-ago), both EUR → currency-consistent %. `get_one_hour_momentum(conn, symbol)` takes NO current_price and NO exchange param. This removes the USD(Alpaca)/EUR(Kraken) FX mismatch entirely.
- Schema: `asset_prices(id BIGSERIAL, symbol TEXT, price DOUBLE PRECISION, recorded_at TIMESTAMPTZ DEFAULT now())`, index `(symbol, recorded_at DESC)`. No `exchange` column (optional `source` for provenance).
- Window for "past": `now()-75min .. now()-55min`, pick closest to 60-min mark.
- Fail-open: all DB ops try/except → fall back to legacy daily momentum.

## Module: traders/extreme/db_prices.py
`base_symbol`, `get_connection`, `ensure_schema`, `insert_prices(conn, price_map)` (Kraken only), `get_one_hour_momentum(conn, symbol)`, `close_connection`.

## Integration
- Kraken: insert `{base_symbol(sym): ticker['last']}` after `fetch_tickers` (every cycle). Reads momentum too.
- Alpaca: only reads momentum by base coin; no insert import.

## Qualification/sizing (both)
- Qualify: `daily>=2.0` OR `hourly>=1.5`.
- Daily tiers 5/3/2 → mult 1.0/0.67/0.33. Hourly tiers 3/2/1.5 → same mults. Take stronger.
- Rotation: `daily>=2.5` OR `hourly>=2.0`. Sort by `effective_pct=max(daily, hourly or -inf)`.

## Open questions
- Confirm thresholds; confirm pre-existing table DDL if any; ensure Kraken cadence ≤~15min so window + latest row stay fresh; Alpaca hourly is None until Kraken has ~1h history.
