# Fixes for Opus Review Findings

Apply the following fixes to the 3 new strategy files. Read each file fully before editing.

## Fix 1 (CRITICAL): kraken_pullback.py — filter positions to state-owned symbols

**Problem:** Line ~439-450 builds `positions` from ALL coins in Kraken balance. On a shared wallet with momentum, pullback adopts and can sell momentum's coins.

**Fix:** After building `positions` (around line 448), filter to only symbols that exist in `state`:
```python
positions = [p for p in positions if p["symbol"] in state]
```
(Mirror what momentum.py does at ~line 412: `my_positions = [p for p in all_positions if p["symbol"] in state]`)

Also fix `get_entry_price_and_time` to not return 0.0 when fetching trades from the other strategy.

## Fix 2 (MAJOR): kraken_pullback.py — ZeroDivisionError on 0.0 entry_price

**Problem:** Line ~461: `if "entry_price" not in new_state[sym]:` only checks key presence, not falsy. A stored `0.0` from DB passes → `(current - 0)/0` = crash.

**Fix:** Match momentum's guard (line ~424 in momentum):
```python
if not new_state[sym].get("entry_price"):
```

## Fix 3 (MAJOR): alpaca_stocks.py — add lock file + crash wrapper

**Problem:** No flock guard and no try/except main(). Overlapping crons can cause double entries.

**Fix:** Add at the top (after imports, before `# ── Config ──`):
```python
import fcntl

LOCK_FILE = "PROJECT_ROOT/logs/alpaca_stocks.lock"
lock_fd = open(LOCK_FILE, "w")
try:
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except IOError:
    print("Another alpaca_stocks instance is running — exiting.")
    sys.exit(0)
```

Wrap main logic in try/except and print error + json report on crash.

## Fix 4 (MINOR): kraken_pullback.py — unused import

Remove `import time` (line 25) since it's unused.

## Fix 5 (MINOR): kraken_pullback.py — heartbeat "c" placeholder

Line ~410: change `pos_str = ... else "c"` to `pos_str = ... else ""` (or just skip the pos_str part when empty).

## Fix 6 (MINOR): alpaca_stocks.py — add credential validation

Add before exchange creation:
```python
for k in ["ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_BASE_URL"]:
    if not os.environ.get(k):
        print(f"Missing required env var: {k}", file=sys.stderr)
        sys.exit(1)
```

## Fix 7 (MINOR): alpaca_stocks.py — add AI gate check

Add the same `load_ai_gates()` check that kraken scripts use, before entry logic:
```python
import json
AI_GATE_PATH = "PROJECT_ROOT/ai_overseer/ai_gate.json"
def load_ai_gates():
    try:
        with open(AI_GATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {"script_paused": False}
```
Then check `if gates.get("script_paused"):` — skip entries, print reason, continue monitoring exits.

## Fix 8 (MINOR): alpaca_stocks.py — remove unused imports

Remove `import time` and `base_symbol` if unused.

## IMPORTANT RULES
- Run `python3 -c "import ast; ast.parse(open('...').read()); print('OK')"` after EVERY file edit
- Do NOT run git commands
- Do NOT run the scripts
- Do NOT delete old files
