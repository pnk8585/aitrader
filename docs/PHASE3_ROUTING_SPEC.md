# Phase 3: Regime-Aware Strategy Routing — SPECIFICATION (Pre-Implementation Review)

## Context

Phase 1 added a regime detector (`traders/strategies/regime/detector.py`) that classifies each symbol as `trending`, `ranging`, `crisis`, or `uncertain` using ADX(14), 20d volatility, and 20d return. It writes to `regime_state` table.

Phase 2 added grid trading as a standalone strategy with `USE_REGIME_ROUTING=False`.

Phase 3 **wires the regime detector into all strategy entry points** so that:
- `trending` → Momentum + Pullback active, Grid inactive
- `ranging` → Grid active, Momentum + Pullback inactive
- `crisis` → All strategies inactive (preserve capital)
- `uncertain` → Momentum + Pullback active (default safe behavior)

**EXIT logic always runs regardless of regime.** Only ENTRY is gated.

---

## 1. Architecture

### New Files
```
traders/strategies/regime/
├── router.py          # Strategy routing logic
```

### Modified Files
```
traders/crypto_trades/kraken_momentum.py   # Gate entries on regime
traders/crypto_trades/kraken_pullback.py   # Gate entries on regime
traders/crypto_trades/kraken_grid.py       # Gate entries on regime (enable USE_REGIME_ROUTING)
traders/strategies/grid/config.py          # USE_REGIME_ROUTING = True
app/main.py                                # Add regime to dashboard context
app/templates/dashboard.html               # Show regime badge per symbol
```

### No New Cron Jobs
Regime routing runs inside existing 5-minute cycles. ZERO new crons.

---

## 2. Router Design (traders/strategies/regime/router.py)

```python
"""Strategy router: maps regime → active strategies."""

# Regime → strategy mapping
REGIME_STRATEGIES = {
    "trending":  {"momentum": True,  "pullback": True,  "grid": False},
    "ranging":   {"momentum": False, "pullback": False, "grid": True},
    "crisis":    {"momentum": False, "pullback": False, "grid": False},
    "uncertain": {"momentum": True,  "pullback": True,  "grid": False},
}

def get_active_strategies(regime: str) -> dict:
    """Returns which strategies should be active for this regime.
    
    Falls back to 'uncertain' for unknown regimes (safe default:
    momentum + pullback active, grid inactive).
    """
    return REGIME_STRATEGIES.get(regime, REGIME_STRATEGIES["uncertain"])


def should_enter(db_conn, symbol: str, strategy: str) -> tuple[bool, str]:
    """Check if a strategy should enter for this symbol right now.
    
    Returns (allowed, reason).
    Reads latest regime from regime_state table.
    """
    cur = db_conn.cursor()
    cur.execute(
        """SELECT regime FROM regime_state 
           WHERE symbol = %s ORDER BY updated_at DESC LIMIT 1""",
        (symbol,)
    )
    row = cur.fetchone()
    cur.close()
    
    if not row:
        return True, "no regime data (allow by default)"
    
    regime = row[0]
    active = get_active_strategies(regime)
    
    if active.get(strategy):
        return True, f"regime={regime}, {strategy} active"
    else:
        return False, f"regime={regime}, {strategy} inactive"
```

### Design Decisions
- **Fail-open:** If no regime data exists, allow entry (new symbols, DB gaps)
- **Fail-open on unknown regime:** Maps to `uncertain` (momentum + pullback)
- **Reads from `regime_state` table:** Same table Phase 1 writes to
- **No import of detector.py:** Router is independent; it reads stored regime, doesn't compute it

---

## 3. Strategy Gating

### Pattern (identical for all 3 crons)

Each cron's `run_cycle()` adds a regime check BEFORE entry logic. EXIT logic is untouched.

**kraken_momentum.py:**
```python
from traders.strategies.regime.router import should_enter

# In run_cycle(), for each pair, BEFORE entry logic:
allowed, reason = should_enter(db, pair, "momentum")
if not allowed:
    report.append(f"⏭️ {pair}: {reason}")
    continue  # skip entry, but exit logic below still runs for existing positions
```

**kraken_pullback.py:**
```python
allowed, reason = should_enter(db, pair, "pullback")
```

**kraken_grid.py:**
```python
# Replace USE_REGIME_ROUTING check with router call:
allowed, reason = should_enter(db, pair, "grid")
if not allowed and grid is None:
    report.append(f"⏭️ {pair}: {reason}")
    continue
```

### Key Rule: EXIT Always Runs
- **Momentum/Pullback:** Exit logic is in a separate section after entry. The `continue` only skips entry, not exits.
- **Grid:** Active grids continue to run cycles (rebalance, hard stop, sell fills) regardless of regime. Only NEW grid creation is gated.
- **Position Monitor:** Unchanged — it's a catch-all for all positions.

---

## 4. Grid Config Change

```python
# traders/strategies/grid/config.py
USE_REGIME_ROUTING = True   # Phase 3 enabled (was False in Phase 2)
```

---

## 5. Dashboard Regime Display

### app/main.py
Add regime to the open positions context:
```python
# When building position data for template:
cur.execute(
    "SELECT regime FROM regime_state WHERE symbol = %s ORDER BY updated_at DESC LIMIT 1",
    (symbol,)
)
regime_row = cur.fetchone()
position["regime"] = regime_row[0] if regime_row else "unknown"
```

### app/templates/dashboard.html
Add regime badge in the Open Positions table:
```html
<td>
    <span class="badge regime-{{ position.regime }}">
        {{ position.regime }}
    </span>
</td>
```

CSS:
```css
.badge { padding: 2px 8px; border-radius: 4px; font-size: 0.85em; }
.regime-trending { background: #27ae60; color: white; }
.regime-ranging { background: #2980b9; color: white; }
.regime-crisis { background: #e74c3c; color: white; }
.regime-uncertain { background: #95a5a6; color: white; }
.regime-unknown { background: #bdc3c7; color: #333; }
```

---

## 6. Safety Guards

1. **Fail-open:** No regime data → allow entry (don't block trading on DB gaps)
2. **Exit always runs:** Regime only gates entry, never exit
3. **Existing positions protected:** Grid continues managing bought positions even if regime changes
4. **Config flag:** Each strategy can independently disable regime routing by setting a local `USE_REGIME_ROUTING = False` override

---

## 7. Implementation Order

1. `traders/strategies/regime/router.py` — NEW
2. `traders/crypto_trades/kraken_momentum.py` — Add regime gate before entry
3. `traders/crypto_trades/kraken_pullback.py` — Add regime gate before entry
4. `traders/crypto_trades/kraken_grid.py` — Add regime gate, remove USE_REGIME_ROUTING check
5. `traders/strategies/grid/config.py` — `USE_REGIME_ROUTING = True`
6. `app/main.py` — Add regime to dashboard
7. `app/templates/dashboard.html` — Show regime badge

---

## 8. Testing Plan

1. **Trending test:** Set regime_state to 'trending' → verify momentum/pullback enter, grid skips
2. **Ranging test:** Set regime_state to 'ranging' → verify grid creates, momentum/pullback skip
3. **Crisis test:** Set regime_state to 'crisis' → verify all entries blocked
4. **Unknown regime:** Set regime_state to 'xyz' → verify fallback to uncertain behavior
5. **No regime data:** Delete regime_state row → verify entry allowed (fail-open)
6. **Exit during regime change:** Open position in trending, change to ranging → verify exit still runs
7. **Grid lifecycle:** Grid active in ranging, change to trending → verify grid continues managing existing levels but stops creating new grids

---

## Decisions

1. **Regime refresh:** No caching — `should_enter()` reads `regime_state` on every call. The data is fresh (5min old max) and it's a single-row SELECT (cheap).
2. **Dashboard scope:** Show regime only for open positions first. Can add "all tracked symbols" view later.
