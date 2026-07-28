-- Phase 5: Enrich Trade Logging
-- Adds regime, ATR, Kelly fraction, and strategy name columns to trade_log

ALTER TABLE trade_log ADD COLUMN IF NOT EXISTS regime VARCHAR(20);
ALTER TABLE trade_log ADD COLUMN IF NOT EXISTS atr_at_entry NUMERIC(20,10);
ALTER TABLE trade_log ADD COLUMN IF NOT EXISTS kelly_fraction NUMERIC(10,6);
ALTER TABLE trade_log ADD COLUMN IF NOT EXISTS strategy_name VARCHAR(50);
