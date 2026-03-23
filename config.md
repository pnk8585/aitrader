# Trading Bot Configuration

## Watchlist
AAPL, NVDA, TSLA, SPY, MSFT, AMZN, GOOGL, META, AMD, NFLX

## Signal Thresholds
- Strong Buy: score >= +7 (multiple corroborating sources, major catalyst)
- Moderate Buy: score >= +4 (single source, positive signal)
- Moderate Sell: score <= -4 (negative development on held ticker)
- Strong Sell: score <= -7 (major negative catalyst)
- Hold: everything else

## Position Rules
- Default trade size: 2% of portfolio equity
- High-conviction trade size: up to 5% of equity
- Max single ticker exposure: 15% of equity
- Max open positions: 10

## Risk Rules
- Stop-loss: exit if position down 7% from entry
- Take partial profit: sell 50% if position up 15% from entry
- Stale position: close if held 3+ trading days with no new supporting news
- Drawdown circuit breaker: stop new entries if portfolio down 10% from peak

## Trading Mode
Paper trading — ALPACA_BASE_URL must be https://paper-api.alpaca.markets
Do not switch to live without explicit instruction.

## Cycle
Run every TRADING_CYCLE_SECONDS (default: 300 seconds / 5 minutes)
