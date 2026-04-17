# ATR Risk-Managed Trader

**Sophisticated trading bot with dynamic risk management using Average True Range (ATR)**

## Overview

This trader implements professional-grade risk management with:
- **Technical analysis only** (no news sentiment analysis)
- **Dynamic ATR-based exits** (14-period ATR)
- **Automatic bracket orders** with stop-loss and take-profit
- **Strict position sizing** (10-15% of equity per trade)
- **Daily drawdown protection** (5% circuit breaker)
- **Limit orders only** (no market orders, no slippage)
- **Fractional share support** for small capital trading

## Risk Management Rules

### Position Sizing
- **Maximum per trade**: 10-15% of total equity
- **Maximum open positions**: 5 (configurable)
- **Daily drawdown limit**: 5% (halts all trading if exceeded)

### ATR-Based Exits
- **Stop-Loss**: `Entry Price - (ATR * 2.0)`
- **Take-Profit**: `Entry Price + (ATR * 4.0)`
- **Risk/Reward Ratio**: 1:2 (minimum)
- **Critical Rule**: Skip trade if SL > 5% of equity (too volatile)

### Order Structure
- **Order Type**: Limit orders only (never market orders)
- **Order Class**: Bracket orders (auto SL/TP)
- **Time in Force**: Day
- **Fractional Shares**: Enabled

## Quick Start

### Prerequisites
```bash
# Install required Python packages
pip install alpaca-py pandas pandas_ta python-dotenv
```

### Environment Variables
Ensure `.env` contains:
```env
ALPACA_API_KEY=your_api_key
ALPACA_SECRET_KEY=your_secret_key
ALPACA_BASE_URL=https://paper-api.alpaca.markets
ALPACA_PAPER=true
TRADER_RUNNER=opencode  # or 'claude'
TRADING_CYCLE_SECONDS=300  # 5 minutes between cycles
```

### Running the Bot

**Single cycle:**
```bash
./run_atr_trader.sh
```

**Continuous loop (Ctrl+C to stop):**
```bash
./run_atr_trader.sh --loop
```

**Or run directly from traders directory:**
```bash
./traders/atr_risk_managed/run_atr_trader.sh
```

## Trading Logic

### 1. Market Check
- Verify market is open
- Sleep if closed

### 2. Account Validation
- Fetch buying power and positions
- Calculate daily P&L
- Circuit breaker: Halt if daily loss >5%

### 3. Data Fetch
- Download 50-100 bars (5min or 15min timeframe)
- Target symbols: NVDA, SPY, BTC/USD, etc.

### 4. ATR Calculation
```python
import pandas_ta as ta
df['atr_14'] = ta.atr(df['high'], df['low'], df['close'], length=14)
current_atr = df['atr_14'].iloc[-1]
```

### 5. Signal Generation
- Apply core strategy (RSI + ATR, Mean Reversion, Trend Following)
- Generate BUY/SELL/HOLD signals

### 6. Risk Validation
- Check signal strength
- Verify max position count
- Validate SL does not exceed 5% of equity
- Confirm R/R ratio >= 1:2

### 7. Execution
```python
# Bracket order with auto SL/TP
trading_client.submit_order(
    symbol=symbol,
    side=OrderSide.BUY,
    type=OrderType.LIMIT,
    qty=qty,
    limit_price=limit_price,
    time_in_force=TimeInForce.DAY,
    order_class=OrderClass.BRACKET,
    stop_loss={'stop_price': sl_price, 'limit_price': sl_price},
    take_profit={'limit_price': tp_price}
)
```

### 8. Cool-off
- Wait for next candle close (5min or 15min)
- Rate limiting: 60s delay on 429 errors

## Safety Protocols

### No Overnight Holdings
- **Market Close**: 16:00 ET (23:00 Greece time)
- **Close Positions By**: 22:45 Greece time
- **Action**: Close all positions 15 min before market close

### Error Handling
- Log all API errors with timestamps
- Rate limit (429): Wait 60 seconds, then retry
- Account errors: Halt and notify
- Position errors: Log and continue

## Logging Format

Trade logs are saved in `logs/trades-YYYY-MM-DD.jsonl`:

```json
{
  "timestamp": "2026-04-17T12:00:00Z",
  "action": "BUY",
  "ticker": "NVDA",
  "strategy": "RSI_ATR",
  "entry_price": 850.50,
  "atr_value": 12.30,
  "sl_price": 825.90,
  "tp_price": 899.70,
  "position_size_pct": 0.15,
  "risk_pct": 0.029,
  "signal_strength": "STRONG_BUY"
}
```

## Key Differences from Other Traders

| Feature | Standard | Aggressive | Extreme | **ATR Risk-Managed** |
|---------|----------|-----------|---------|---------------------|
| Analysis Type | News + Tech | News + Tech | Tech Only | **Tech Only** |
| Stop-Loss | Fixed -5% | Fixed -5% | Fixed -5% | **Dynamic (ATR × 2)** |
| Take-Profit | Fixed +10% | Fixed +5% | None | **Dynamic (ATR × 4)** |
| Order Type | Market | Market | Market | **Limit only** |
| Bracket Orders | No | No | No | **Yes (auto SL/TP)** |
| ATR Analysis | No | No | No | **Yes (14-period)** |
| Risk/Reward Ratio | 2:1 | 1:1 | None | **1:2 (minimum)** |
| Daily Drawdown Limit | 15% | 15% | 15% | **5% (stricter)** |
| Fractional Shares | Yes | Yes | Yes | **Yes (enabled)** |
| No Overnight Hold | No | No | No | **Yes (22:45 cutoff)** |

## Customization

### Change Trading Timeframe
Edit `trading_prompt.md`:
```python
# Fetch 5-minute bars
bars = trading_client.get_bars(
    symbol=symbol,
    timeframe=TimeFrame.FiveMinute,
    start=start_time,
    end=end_time,
    limit=100
)
```

### Adjust Risk Parameters
Edit `trading_prompt.md`:
```python
# Change position size
position_value = equity * 0.10  # 10% instead of 15%

# Change ATR multiplier
sl_price = entry_price - (atr * 1.5)  # Tighter SL
tp_price = entry_price + (atr * 3.0)  # Tighter TP
```

### Change Strategy
The ATR calculations remain the same, but you can modify the signal generation logic:
- RSI + ATR breakout
- Mean reversion with ATR bands
- Trend following with ATR trailing stops
- Or your own custom strategy

## Disclaimer

**This bot is for educational and paper trading purposes only.**

High-leverage and high-frequency trading involve significant risk of capital loss. Never trade with money you cannot afford to lose. Always backtest thoroughly before using real money.

## Troubleshooting

**Issue**: "Risk too high: 6.2%"
- **Solution**: Skip the trade, asset is too volatile for your capital size

**Issue**: Rate limit errors (429)
- **Solution**: Bot automatically waits 60 seconds and retries. Increase `TRADING_CYCLE_SECONDS` if persistent

**Issue**: Positions not closing at market close
- **Solution**: Check server timezone. Market close is 16:00 ET (23:00 Greece time)

## Files

- `trading_prompt.md` — System prompt for AI agent
- `run_atr_trader.sh` — Runner script (in traders directory)
- `../../run_atr_trader.sh` — Root runner script (convenience)
- `logs/` — Trade logs (JSONL format)

## Support

For issues or questions, check the main project README or logs in `logs/` directory.