# Opus: Εφάρμοσε τις 5 αλλαγές

Βρίσκεσαι στο `PROJECT_ROOT/` — Kraken trading bot με 2 scripts:

- `traders/crypto_trades/kraken_pullback.py` — pullback-in-uptrend strategy
- `traders/crypto_trades/kraken_momentum.py` — momentum strategy

## Διάγνωση

**Pullback:** trail arm στο +1.5%, giveback 0.7% → βγαίνει στο +0.8% gross. Stop στο -2.5% min. R:R = 1:4.
**Momentum:** stop -3.5% αλλά trailing TP δίνει +0.52% (breakeven rule στο fee floor). Ασύμμετρο.
**Position sizing:** 97% του portfolio ανά trade.

## Οι 5 αλλαγές

### 1. Pullback trailing — proportional (winners να τρέχουν)
`kraken_pullback.py:90-91`: 
- `TRAIL_ARM_PCT = 1.5` → `TRAIL_ARM_PCT = 2.5`
- `TRAIL_GIVEBACK_PCT = 0.7` → `TRAIL_GIVEBACK_FRAC = 0.40` και `TRAIL_GIVEBACK_MIN_PCT = 1.0`
- L578: αντικατέστησε τη συνθήκη trailing ώστε να χρησιμοποιεί `max(TRAIL_GIVEBACK_MIN_PCT, peak_plpc * TRAIL_GIVEBACK_FRAC)` αντί `TRAIL_GIVEBACK_PCT`
- Διέγραψε `TRAIL_GIVEBACK_PCT` από `ADJUSTMENT_BOUNDS` στον `ai_overseer.py` (line ~64)

### 2. Pullback stop — dynamic, tighter όταν bleeding
`kraken_pullback.py:88`: `MIN_HARD_STOP_PCT = 2.5` → `MIN_HARD_STOP_PCT = 2.0`
L570-572: Πρόσθεσε `realized_pnl_today_pct()` helper και dynamic tightening: `stop_tighten = 0.6 if rpnl_today <= -2.0 else 1.0`

### 3. R:R entry gate — απαιτεί upside ≥ 2× stop dist
Πρόσθεσε `RR_MIN = 2.0` κοντά στα entry constants.
Μετά το pullback check (μετά L721), πρόσθεσε R:R gate που συγκρίνει room-to-6h-high vs stop distance.

### 4. Momentum stop & breakeven
`kraken_momentum.py:80`: `STOP_LOSS_PCT = -3.5` → `-2.5`
`kraken_momentum.py:81`: `BREAKEVEN_PEAK_PCT = 1.0` → `2.0`

### 5. Position size — 97% → 60%
`kraken_pullback.py:97`: `DEPLOY_FRACTION = 0.97` → `0.60`
`kraken_momentum.py:89`: `DEPLOY_FRACTION = 0.97` → `0.60`

## Εντολή
1. Διάβασε και τα 2 source files + ai_overseer.py
2. Δες αν υπάρχει συνάρτηση `realized_pnl_today_pct()` (αν όχι, φτιάξε την)
3. Εφάρμοσε ΟΛΕΣ τις 5 αλλαγές
4. Μετά από κάθε αλλαγή: `python3 -c "import ast; ast.parse(open('file').read())"` → verify syntax
5. `git add -A && git commit -m "opus: apply 5 fixes — trailing, stop, R:R gate, momentum, sizing" && git push`
6. Πες μου ακριβώς τι άλλαξες (line numbers, old/new)
