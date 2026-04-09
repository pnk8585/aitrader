# Extreme Trader Mode

## Overview

The Extreme Trader is a **pure momentum-based trading strategy** that completely ignores news and fundamental analysis. It focuses exclusively on price action and intraday momentum.

## Key Characteristics

| Feature | Value |
|---------|-------|
| **Strategy** | Pure momentum / price action |
| **News-based?** | NO — does not fetch or read news |
| **Profit Target** | 2% (sell immediately when reached) |
| **Stop-Loss** | -5% (cut losses fast) |
| **Max Hold Time** | 2 hours (rotate if stale) |
| **Max Positions** | 5 open positions |
| **Scan Frequency** | Every 60 seconds (configurable) |
| **Crypto Support** | Yes (fallback when no stocks or market closed) |
| **Cash Check** | Only scans if buying power > 0 |

## How It Works

### 1. Position Management (ALWAYS FIRST)
**CRITICAL: The extreme trader ALWAYS checks positions first, before any new purchases.**
- Checks all existing positions
- **Sells immediately** if position is up 2% or more (profit target)
- **Sells immediately** if position is down 5% (stop-loss)
- **Closes** any position held longer than 2 hours
- Frees up capital when positions hit targets

### 2. Check Buying Power
**Only after managing positions:**
- Check if `buying_power > 0`
- **If NO cash available** → Skip scanning, just monitor existing positions
- **If cash available** → Proceed to momentum scanning
- This prevents scanning for opportunities when you can't act on them

### 3. Momentum Scanning (Stocks)
Only if buying power available AND market is open:
- Scans high-volatility stocks: TSLA, NVDA, AMD, MSTR, COIN, SMCI, PLTR, etc.
- Calculates intraday momentum: `(latest_price - open_price) / open_price`
- Ranks by momentum percentage

### 4. Momentum Criteria
| Signal | Criteria | Position Size |
|--------|----------|---------------|
| **EXTREME** | >5% intraday gain | Full allocation (MAX_POSITION_PCT) |
| **STRONG** | 3-5% intraday gain | 67% allocation |
| **MODERATE** | 2-3% intraday gain | 33% allocation |

### 5. Crypto Fallback
Only if buying power available AND (no stocks meet criteria OR stock market is closed):
- Scans crypto pairs: BTC/USD, ETH/USD, SOL/USD, AVAX/USD, LINK/USD
- Same momentum criteria apply
- Crypto trades 24/7

### 6. Rapid Rotation
- After selling at 2% profit, capital is freed up
- In next cycle, buying power check passes → scan for new opportunities
- No waiting — continuous momentum hunting
- Goal: Compound small 2% gains rapidly

## Execution Flow

```
1. Check existing positions
   └── Sell if 2% profit / -5% loss / >2 hours held
2. Check buying power
   └── If 0 → End cycle (monitoring only)
   └── If >0 → Continue
3. Scan stocks for momentum
   └── Buy strongest if found
4. If no stocks → Scan crypto
   └── Buy strongest if found
5. Log and wait for next cycle
```

## Usage

```bash
# Run single cycle
./run_extreme.sh

# Run continuously (fast rotation)
./run_extreme.sh --loop
```

## Configuration

Add to `.env`:

```bash
# Extreme trader runner and model
EXTREME_TRADER_RUNNER=opencode              # or 'claude'
EXTREME_TRADER_MODEL=zai-coding-plan/glm-5.1  # Fast, efficient for momentum scanning
EXTREME_TRADER_INTERVAL_SECONDS=60          # Scan interval (default: 60s)
```

## Comparison with Other Traders

| Aspect | Regular Trader | Aggressive Trader | Extreme Trader |
|--------|---------------|-------------------|----------------|
| **Uses News?** | Yes | Yes | **NO** |
| **Strategy** | News + Fundamentals | News + Momentum | **Pure Momentum** |
| **Profit Target** | 10-30% tiered | 2% | **2%** |
| **Max Hold** | 1 trading day | 4 hours | **2 hours** |
| **Scan Frequency** | 10 minutes | 5 minutes | **1 minute** |
| **Crypto** | Yes | Yes | **Yes** |
| **Buys Without Cash?** | No | No | **NO — checks first** |

## Files

- `run_extreme.sh` — Runner script
- `agents/extreme-trader.md` — Agent system prompt
- `extreme_trading_prompt.md` — Trading logic and rules

## Risks

- **Pure momentum trading** can result in buying at peaks
- **Fast rotation** increases transaction frequency (watch for fees in live trading)
- **No news filter** means buying into pumps without knowing catalysts
- **2% profit target** may be too tight during strong trends (leaving money on table)
- **2-hour max hold** may force selling winners before they run

## When to Use

- **High volatility markets** with clear intraday trends
- **Small accounts** where 2% gains compound quickly
- **Momentum trading days** when news is lagging price action
- **24/7 operation** using crypto when stocks are closed

## When NOT to Use

- **Choppy/sideways markets** with no clear momentum
- **Low volatility periods** where 2% moves are rare
- **News-driven events** where fundamentals matter more than price action
- **Large accounts** where transaction costs matter more
