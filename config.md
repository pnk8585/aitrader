# Trading Bot Configuration — Aggressive Mode

## Target
5–10% portfolio growth per week through concentrated, catalyst-driven trades.

## Watchlist (high-volatility focus)
TSLA, NVDA, AMD, MSTR, COIN, SMCI, PLTR, ROKU, SNAP, SHOP

## Signal Thresholds
- Extreme Buy:   enter 15% of equity (earnings beat >10%, M&A target, FDA approval)
- Strong Buy:    enter 10% of equity (earnings beat, analyst upgrade, guidance raise)
- Moderate Buy:  enter 5% of equity  (positive news, single source)
- Moderate Sell: reduce position 50%
- Strong Sell:   exit full position immediately

## Position Rules
- Max 5 open positions (concentrate, don't diversify)
- Max 25% of equity in one ticker
- Min average daily volume: 1,000,000 shares

## Risk Rules
- Stop-loss:        exit if position down 5% from entry (no exceptions)
- Partial profit:   sell 50% at +10%, trail stop on rest
- More profit:      sell 25% more at +20%
- Full exit:        close at +30%
- Time stop:        close flat/losing positions after 1 trading day with no new catalyst
- Daily limit:      stop trading if 3 stop-losses triggered in one day
- Circuit breaker:  halt new entries if portfolio drawdown exceeds 15% from peak
- Resume:           only when drawdown recovers below 8%

## Cycle
TRADING_CYCLE_SECONDS=60 (every 60 seconds during market hours)

## Trading Mode
Paper trading — ALPACA_BASE_URL must be https://paper-api.alpaca.markets
Do not switch to live without explicit instruction.
