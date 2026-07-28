-- Phase 2: Grid Trading Strategy — DB schema
-- Run: psql -U aitrader -d aitrader -f scripts/phase2_migration.sql

CREATE TABLE IF NOT EXISTS grid_state (
    id SERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL DEFAULT 'kraken',
    grid_low NUMERIC NOT NULL,
    grid_high NUMERIC NOT NULL,
    num_grids INTEGER NOT NULL DEFAULT 10,
    capital_allocated NUMERIC NOT NULL,
    levels JSONB NOT NULL DEFAULT '[]',
    total_buys INTEGER DEFAULT 0,
    total_sells INTEGER DEFAULT 0,
    realized_pnl NUMERIC DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(symbol, exchange)
);

CREATE INDEX IF NOT EXISTS idx_grid_state_symbol ON grid_state(symbol, exchange);
CREATE INDEX IF NOT EXISTS idx_grid_state_status ON grid_state(status);
