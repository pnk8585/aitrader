# AITrader Strategy Research
> Generated: 2026-07-28 | Sources: Web research + codebase analysis

---

## Current System (as-is)

| Strategy | Type | Entry Signal | Exit Logic | Mode |
|----------|------|-------------|------------|------|
| **Momentum** | Trend-following | RSI, ATR, 3h/6h momentum, EXTREME_MOMENTUM | Trailing stop, time-stop, breakeven protection | **live** |
| **Pullback** | Mean-reversion | 3h uptrend ≥3% + pullback ≥3% from high | Hard stop -2%, trailing, time-stop 72h | **live** |
| **Position Monitor** | Catch-all | No entries — exits only | LLM decides SELL/HOLD every 2h, hard stop -15% | **live** |
| **Alpaca Stocks** | Momentum (US) | EXTREME_MOMENTUM intraday | Stop-loss -2%, time-stop 72h, breakeven | **live** |

### Current Gaps
- **No regime detection** — momentum bleeds in sideways, pullback finds nothing without trend
- **Fixed position sizing** — `DEPLOY_FRACTION=0.12` regardless of conviction/volatility
- **Binary exits** — hold ALL or sell ALL, no partial profit-taking
- **Static stops** — -2% hard stop regardless of ATR/volatility
- **No grid/range strategy** — idle capital when market is sideways (July 24: all coins 0% momentum)

---

## Strategy #1: Regime Detection ⭐ TOP PRIORITY

### Problem It Solves
July 24-28 data: all 12 coins at 0% momentum, 1.4% max range. Both momentum and pullback sat idle. A regime detector would have switched to grid/mean-reversion mode.

### How It Works
Classifies market into 3 states, routes to appropriate strategy:

| State | Indicators | Active Strategy |
|-------|-----------|-----------------|
| **Trending** | ADX > 25, abs(return_20d) > 5%, rising vol | Momentum ON |
| **Ranging** | ADX < 20, vol < 15%, narrow range | Grid/Mean-reversion ON |
| **Crisis** | Vol > 30%, correlation spike, liquidity drop | Cash only, reduce exposure |

### Implementation (Rules-Based — simplest, most practical)
```python
def detect_regime(prices_20d):
    adx = compute_adx(prices_20d, period=14)
    vol_20d = annualized_volatility(prices_20d)
    ret_20d = (prices_20d[-1] / prices_20d[0] - 1) * 100
    
    if vol_20d > 30:
        return "crisis"
    elif adx > 25 and abs(ret_20d) > 5:
        return "trending"
    elif adx < 20 and vol_20d < 15:
        return "ranging"
    else:
        return "uncertain"  # keep current strategy
```

### Key Insight
> "The market is always in some state. The edge comes from knowing WHEN to use which strategy." — Waylandz, AI Quantitative Trading

A quant fund ran momentum + mean-reversion with 50/50 fixed weights → 0% annual return. Same strategies with regime-based weighting → 18% annual, Sharpe 1.6.

### References
- [Regime Detection Lesson](https://waylandz.com/quant-book-en/Lesson-12-Regime-Detection/)
- [Volatility Ratio Regime-Switching in Python](https://pyquantlab.medium.com/volatility-ratio-mean-reversion-vs-momentum-regime-switching-strategy-in-python-95ba453e03f6)
- [Hidden Markov Models for Regime Detection](https://www.cube.exchange/what-is/market-regime-detection-with-hidden-markov-models)

---

## Strategy #2: Grid Trading

### Problem It Solves
Sideways markets (most common state in crypto). When price oscillates in a range, grid bots profit on every swing without predicting direction.

### How It Works
1. Define price range (e.g., AVAX €5.50 - €6.20)
2. Place N evenly-spaced grid levels (e.g., 15 levels)
3. Buy order below each level, sell order above
4. When price crosses down → buy. Crosses up → sell.
5. Each completed cycle = small profit

### Best Conditions
- ✅ Sideways/range-bound markets
- ✅ High volatility WITHIN the range
- ⚠️ Mild trends (still profitable if in range)
- ❌ Strong breakouts (bot gets stuck holding bags)

### For AITrader
- Activate via regime detector when market = "ranging"
- Use the 12 coins already scanned by pullback strategy
- Auto-adjust range based on 30-day support/resistance

### References
- [Grid Trading Strategy 2026 — Bitsgap](https://bitsgap.com/blog/grid-trading-strategy-explained-how-to-profit-in-any-market-in-2026)
- [Futures Grid Trading — Kraken](https://www.kraken.com/learn/futures-grid-trading-bots)
- [Cornix Grid Trading](https://cornix.io/grid-trading-explained-trading-volatility-in-sideways-markets/)

---

## Strategy #3: Kelly Criterion Position Sizing

### Problem It Solves
Fixed 12% deploy fraction doesn't account for conviction level, win rate, or risk/reward. Kelly optimizes growth rate over many trades.

### Formula
```
f* = (b × p - q) / b

b = reward-to-risk ratio (e.g., 1.5:1)
p = win probability (e.g., 0.55)
q = 1 - p (e.g., 0.45)
```

### Example
Win rate 55%, R:R 1.5:1:
```
f* = (1.5 × 0.55 - 0.45) / 1.5 = 0.25 (25%)
```

### Important: Use Quarter-Kelly in Crypto
Full Kelly assumes perfect inputs and independent trades. In crypto:
- **Quarter-Kelly** (6-7% per trade) = smooth equity curve
- **Half-Kelly** (12-13%) = moderate risk
- **Full-Kelly** (25%) = max growth but wild swings

### For AITrader
Replace fixed `DEPLOY_FRACTION=0.12` with dynamic Kelly based on:
- Historical win rate from `trade_log` table (last 50 trades)
- Average R:R from closed positions
- LLM confidence score as a multiplier

### References
- [Kelly Criterion for Crypto — Altrady](https://www.altrady.com/blog/risk-management/kelly-criterion-crypto-position-sizing)
- [Kelly Criterion — Wikipedia](https://en.wikipedia.org/wiki/Kelly_criterion)
- [Position Sizing Guide](https://www.fullswing.ai/blog/crypto-position-sizing-guide)

---

## Strategy #4: ATR-Based Dynamic Stops

### Problem It Solves
Fixed -2% stop doesn't account for volatility. In high-vol periods, you get stopped out by noise. In low-vol, the stop is too loose.

### Implementation
```python
# Instead of: stop = entry * 0.98 (fixed -2%)
# Use:
atr = compute_atr(ohlcv, period=14)
stop = entry - (2.0 * atr)  # 2x ATR below entry

# Example:
# AVAX entry €5.76, ATR=0.12 → stop = €5.52 (-4.2%)
# AVAX entry €5.76, ATR=0.05 → stop = €5.66 (-1.7%)
```

### Existing Support
`ATR_PERIOD = 14` and `MAX_ATR_PCT = 3.5` already exist in momentum config. Just need to wire it into pullback stops.

### References
- [ATR Stop Loss Guide](https://www.altrady.com/crypto-trading/risk-management/set-stop-loss-take-profit-levels)
- [Binance Exit Strategies](https://www.binance.com/en/academy/articles/5-exit-strategies-for-traders)

---

## Strategy #5: Laddered Take-Profit (Partial Sells)

### Problem It Solves
Currently: hold 100% until exit signal → sell 100%. Misses locking profits on the way up.

### Implementation
| Level | Trigger | Action | Example (entry €5.00) |
|-------|---------|--------|----------------------|
| TP1 | +5% | Sell 25% | Πουλάς @ €5.25 |
| TP2 | +10% | Sell 25% | Πουλάς @ €5.50 |
| TP3 | +15% | Sell 25% | Πουλάς @ €5.75 |
| TP4 | +25% | Sell 25% | Πουλάς @ €6.25 |

After TP1, move stop to breakeven. After TP2, move stop to TP1 level.

### References
- [Laddered Exits Guide](https://www.altrady.com/blog/crypto-trading-strategies/when-to-take-profits-crypto-trading)
- [Gate.io TP/SL Master Guide](https://www.gate.com/crypto-wiki/article/comprehensive-guide-to-take-profit-and-stop-loss-securing-gains-and-managing-risk-20260119)

---

## Strategy #6: DCA Entry (Dollar-Cost Averaging into Positions)

### Problem It Solves
Momentum signal fires → buy 100% at once. If price drops 3% after, you're underwater immediately.

### Implementation
Instead of buying 100% on signal:
- **50%** on signal trigger
- **25%** if price drops 3% from entry
- **25%** if price drops another 3%

Result: lower average entry if the market gives you a better price.

### References
- [DCA vs Lump Sum — Binance](https://www.binance.com/en/square/post/27975319922058)
- [DCA Strategy Guide](https://www.sofi.com/learn/content/crypto-dollar-cost-averaging/)

---

## Strategy #7: Mean Reversion with Bollinger Bands

### Problem It Solves
Pullback strategy requires a 3h uptrend — misses opportunities when price is range-bound but volatile.

### How It Works
- Buy when price touches lower Bollinger Band (2σ)
- Sell when price returns to middle band (20 SMA)
- Works best in ranging markets

### For AITrader
Complement pullback strategy: pullback needs trend + dip, Bollinger just needs range + dip.

### References
- [Bollinger Band Strategy](https://www.hashcodex.com/crypto-trading-bot-strategies#bollinger-band-strategy)
- [Mean Reversion Strategies](https://paperswithbacktest.com/strategies/mean-reverting-vs-momentum-strategies)

---

## Strategy #8: Multi-Timeframe Confluence

### Problem It Solves
Single timeframe can give false signals. Confirmation across timeframes reduces noise.

### Implementation
- **1h**: Entry timing (momentum/pullback signal)
- **4h**: Trend confirmation (EMA direction)
- **1d**: Major trend filter (only trade in direction of daily trend)

Rule: Only enter if ALL 3 timeframes agree on direction.

### References
- [Multi-Timeframe Trading — AudaCity Capital](https://audacity.capital/trading-guides/most-profitable-trading-strategies/)

---

## Proposed Architecture

```
┌─────────────────────────────────┐
│        REGIME DETECTOR          │
│  ADX + Volatility + Return      │
│  → trending / ranging / crisis  │
└──────────┬──────────────────────┘
           │
    ┌──────┼──────────┐
    ▼      ▼          ▼
MOMENTUM  GRID    PULLBACK
(trend)  (range)  (trend+pullback)
    │      │          │
    └──────┼──────────┘
           ▼
    POSITION MONITOR (catch-all)
    LLM decides exits every 2h
           │
           ▼
    KELLY SIZING + LADDERED TP + ATR STOPS
```

## Implementation Priority

| # | Strategy | Effort | Impact | Prerequisite |
|---|----------|--------|--------|-------------|
| 1 | Regime Detection | Medium | 🔥🔥🔥 | None — enables everything else |
| 2 | Grid Trading | Medium | 🔥🔥 | Needs regime detector |
| 3 | ATR-Based Stops | Low | 🔥 | None |
| 4 | Kelly Sizing | Low | 🔥 | Needs trade history |
| 5 | Laddered TP | Low | 🔥 | None |
| 6 | DCA Entry | Low | 🔥 | None |
| 7 | Bollinger Mean Reversion | Low | 🔥 | Regime detector helps |
| 8 | Multi-Timeframe | Medium | 🔥 | None |

---

## Open-Source Trading Bot References
> Source: [CoinCodeCap — 5 Best Open-Source Trading Bots on GitHub](https://coincodecap.com/open-source-trading-bots-on-GitHub) (June 2026)

### Top 6 Bots Compared

| Bot | Stars | Language | Best For | Kraken? | Grid? | DCA? | ML? |
|-----|-------|----------|----------|---------|-------|------|-----|
| **Freqtrade** | 25,000+ | Python | ML strategy development | ✅ CCXT | ✅ | ✅ | ✅ FreqAI |
| **Hummingbot** | 6,000+ | Python | Market making / HFT | ✅ | ❌ | ❌ | ❌ |
| **OctoBot** | 5,400+ | Python | Beginners + AI | ✅ | ✅ | ✅ | ✅ |
| **Jesse** | 5,000+ | Python | Backtesting + research | ✅ | ❌ | ❌ | ✅ |
| **Superalgos** | 4,000+ | JavaScript | Visual / no-code | ✅ | ✅ | ✅ | ❌ |
| **OpenTrader** | Growing | TypeScript | DCA + Grid with UI | ✅ CCXT | ✅ | ✅ | ❌ |

### Detailed Analysis

#### Freqtrade ⭐ #1 Pick
- **25,000+ GitHub stars**, most actively developed
- **FreqAI**: ML integration — train models on historical data, auto-retrain as market shifts
- **HyperOpt**: brute-force parameter optimization across thousands of combinations
- **Dry-run mode**: paper trades against live exchange feed
- **30+ exchanges** via CCXT (Kraken ✅)
- **Telegram + WebUI control**
- **Setup time**: 30-90 min, real learning curve
- **License**: GPL-3.0 (restricts commercial use)
- **Relevance to AITrader**: FreqAI could replace our LLM-based decision making for exits. HyperOpt could optimize our momentum/pullback parameters.

#### Jesse ⭐ Best Backtesting
- **Zero look-ahead bias** — most honest backtest engine in open-source
- **Monte Carlo stress testing**
- **JesseGPT**: AI assistant for strategy development
- **ML pipeline** built-in
- **Direct Kraken API** (not CCXT — more stable)
- **License**: MIT (most permissive)
- **Relevance to AITrader**: Backtest our strategies properly before going live. Jesse's zero-lookahead bias is critical — our current backtests may be unreliable.

#### Hummingbot
- **Purpose-built for market making** and cross-exchange arbitrage
- **50+ exchange connectors** (DEX + CEX)
- **Liquidity mining** campaigns
- **License**: Apache-2.0
- **Relevance to AITrader**: Market making on small-cap EUR pairs could generate yield on idle capital. High complexity though.

#### OctoBot
- **No Python required** — click-and-configure
- **Managed cloud** from $9.99/mo
- **Pre-built strategies** including DCA, Grid, AI
- **License**: GPL-3.0
- **Relevance to AITrader**: Good for quick grid bot deployment without writing code. We already have more sophisticated infra though.

### What We Can Steal From These

| From Bot | What to Steal | Difficulty |
|----------|--------------|------------|
| **Freqtrade** | FreqAI for exit decisions (replace/augment our LLM) | High |
| **Freqtrade** | HyperOpt for parameter optimization | Medium |
| **Jesse** | Backtesting engine with zero look-ahead bias | Medium |
| **Jesse** | Monte Carlo stress testing for our strategies | Low |
| **OctoBot** | Grid bot logic (ready-made, Python) | Low |
| **Hummingbot** | Market making spread logic | High |
| **OpenTrader** | DCA entry logic | Low |

### Most Interesting for AITrader: Freqtrade
We could potentially **integrate FreqAI** as a replacement for our current LLM-based position monitor decisions. FreqAI trains on actual historical data rather than relying on an LLM's general knowledge.

**But**: Our system is already custom-built with ccxt, PostgreSQL, cron orchestrator, and Telegram integration. Extracting individual components (FreqAI, HyperOpt) is more realistic than replacing the whole thing.

### License Considerations
- **GPL-3.0** (Freqtrade, OctoBot): If we modify and distribute, must open-source our modifications
- **Apache-2.0** (Hummingbot, Superalgos): More permissive, can keep modifications private
- **MIT** (Jesse, OpenTrader): Most permissive, do whatever you want

---

## References

### Strategies
- [Best Crypto Trading Strategies 2026 — Coinspot](https://coinspot.io/en/trading/best-crypto-trading-strategies/)
- [Crypto Bot Strategies 2026 — HashCodex](https://www.hashcodex.com/crypto-trading-bot-strategies)
- [10 Most Profitable Trading Strategies 2026 — AudaCity](https://audacity.capital/trading-guides/most-profitable-trading-strategies/)

### Risk Management
- [Stop-Loss & Take-Profit Guide — TheCryptoDash](https://thecryptodash.com/stop-loss-and-take-profit-strategies-for-cryptocurrency)
- [Kelly Criterion for Crypto — Altrady](https://www.altrady.com/blog/risk-management/kelly-criterion-crypto-position-sizing)
- [Binance Exit Strategies](https://www.binance.com/en/academy/articles/5-exit-strategies-for-traders)

### Regime Detection
- [Regime Detection Lesson — Waylandz](https://waylandz.com/quant-book-en/Lesson-12-Regime-Detection/)
- [Volatility Ratio Regime-Switching — PyQuantLab](https://pyquantlab.medium.com/volatility-ratio-mean-reversion-vs-momentum-regime-switching-strategy-in-python-95ba453e03f6)
- [HMM Regime Detection — Cube Exchange](https://www.cube.exchange/what-is/market-regime-detection-with-hidden-markov-models)

### Grid Trading
- [Grid Trading 2026 — Bitsgap](https://bitsgap.com/blog/grid-trading-strategy-explained-how-to-profit-in-any-market-in-2026)
- [Grid Trading — Cornix](https://cornix.io/grid-trading-explained-trading-volatility-in-sideways-markets/)

### Frameworks & Tools
- [Open-Source Trading Bots on GitHub — CoinCodeCap](https://coincodecap.com/open-source-trading-bots-on-GitHub)
- [Jesse Trading Framework](https://jesse.trade/)
- [Freqtrade](https://www.freqtrade.io/)
- [Quantpedia — 900+ Strategies](https://quantpedia.com/)
