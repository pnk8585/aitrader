-- Phase 1: Regime detection + laddered TP state columns
-- Run: psql $DATABASE_URL -f scripts/phase1_migration.sql

CREATE TABLE IF NOT EXISTS regime_state (
    id SERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    regime TEXT NOT NULL,
    adx_14 NUMERIC,
    vol_20d NUMERIC,
    ret_20d NUMERIC,
    computed_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_regime_symbol
    ON regime_state(symbol, computed_at DESC);

ALTER TABLE trading_state
    ADD COLUMN IF NOT EXISTS tp_level INT DEFAULT 0;

ALTER TABLE trading_state
    ADD COLUMN IF NOT EXISTS tp_sold_qty NUMERIC DEFAULT 0;

ALTER TABLE trading_state
    ADD COLUMN IF NOT EXISTS dca_level INT DEFAULT 0;

ALTER TABLE trading_state
    ADD COLUMN IF NOT EXISTS signal_price NUMERIC;
