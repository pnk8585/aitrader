#!/usr/bin/env python3
"""
ai_overseer.py — Hourly AI agent that evaluates opportunities the script might
have missed, finds setups outside the script's scope, adjusts strategy params,
and can execute trades directly.

Runs alongside the 5-min execute_kraken_cycle_v2.py cycle.
"""
import os
import sys
import json
import re
import ast
import ccxt
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from openai import OpenAI

# --- Paths -----------------------------------------------------------------
PROJECT_DIR = "PROJECT_ROOT"
V2_SCRIPT = os.path.join(PROJECT_DIR, "traders/extreme/execute_kraken_cycle_v2.py")
ENV_PATH = os.path.join(PROJECT_DIR, ".env")
HERMES_ENV = "HOME/.hermes/.env"
LOG_DIR = os.path.join(PROJECT_DIR, "logs/ai_overseer")
os.makedirs(LOG_DIR, exist_ok=True)

load_dotenv(dotenv_path=ENV_PATH)

# --- DeepSeek key (from Hermes env) ----------------------------------------
load_dotenv(dotenv_path=HERMES_ENV)
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY")
if not DEEPSEEK_KEY:
    print("FATAL: DEEPSEEK_API_KEY not found", file=sys.stderr)
    sys.exit(1)

AI_MODEL = "deepseek-chat"  # aliases to deepseek-v4-flash on api.deepseek.com
DEEPSEEK_BASE = "https://api.deepseek.com/v1"

# --- Kraken exchange -------------------------------------------------------
KRAKEN_API_KEY = os.getenv("KRAKEN_API_KEY")
KRAKEN_SECRET = os.getenv("KRAKEN_SECRET")
if not KRAKEN_API_KEY or not KRAKEN_SECRET:
    print("FATAL: Missing Kraken credentials", file=sys.stderr)
    sys.exit(1)

exchange = ccxt.kraken({
    "apiKey": KRAKEN_API_KEY,
    "secret": KRAKEN_SECRET,
    "enableRateLimit": True,
})
EXCHANGE_NAME = "kraken"

# --- Safety limits ---------------------------------------------------------
MAX_TRADE_SIZE_EUR = 30.0         # max EUR per AI-triggered trade
MAX_POSITIONS_AI = 1               # max positions opened by AI
ADJUSTMENT_BOUNDS = {
    "VOL_FLOOR_PCT": (1.5, 8.0),
    "PULLBACK_MIN_PCT": (0.2, 3.0),
    "BLOWOFF_GUARD_1H_PCT": (2.0, 8.0),
    "MIN_HARD_STOP_PCT": (1.0, 5.0),
    "TRAIL_ARM_PCT": (0.5, 3.0),
    "TRAIL_GIVEBACK_PCT": (0.2, 1.5),
    "HARD_TP_CAP_PCT": (3.0, 15.0),
    "MAX_HOLD_HOURS": (4.0, 48.0),
    "DEPLOY_FRACTION": (0.1, 0.97),
}


# ---------------------------------------------------------------------------
# DB helpers (inline — same pattern as v2)
# ---------------------------------------------------------------------------
def get_db():
    import psycopg2
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
        user=os.getenv("DB_USER", "pank"),
        password=os.getenv("DB_PASSWORD", ""),
        dbname=os.getenv("DB_NAME", "trading"),
    )


def query_all(sql, args=()):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()
    finally:
        conn.close()


def query_one(sql, args=()):
    rows = query_all(sql, args)
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Gather state
# ---------------------------------------------------------------------------
def get_portfolio_value():
    """Latest portfolio_equity from trade_log."""
    row = query_one("SELECT portfolio_equity FROM trade_log "
                     "WHERE exchange=%s ORDER BY timestamp DESC LIMIT 1",
                     (EXCHANGE_NAME,))
    return float(row[0]) if row else None


def get_open_positions():
    """Read current positions from trading_state."""
    rows = query_all(
        "SELECT symbol, entry_price, entry_time, peak_plpc "
        "FROM trading_state WHERE exchange=%s", (EXCHANGE_NAME,))
    return rows


def get_recent_trades(limit=15):
    """Last N trades with details."""
    rows = query_all(
        "SELECT timestamp, action, ticker, entry_price, current_price, "
        "       unrealized_plpc, estimated_value, reason "
        "FROM trade_log WHERE exchange=%s "
        "ORDER BY timestamp DESC LIMIT %s",
        (EXCHANGE_NAME, limit))
    return rows


def get_market_snapshot():
    """Price, 6h range, 3h momentum for each pair."""
    pairs = [
        "BTC/EUR", "ETH/EUR", "SOL/EUR", "AVAX/EUR", "LINK/EUR",
        "XRP/EUR", "DOGE/EUR", "SUI/EUR", "NEAR/EUR", "RENDER/EUR",
        "ADA/EUR", "DOT/EUR",
    ]
    snapshot = []
    tickers = exchange.fetch_tickers()
    for sym in pairs:
        ticker = tickers.get(sym)
        if not ticker or ticker.get("last") is None:
            continue
        price = ticker["last"]
        base = sym.split("/")[0]
        # 6h range
        row6 = query_one(
            "SELECT MIN(price), MAX(price) FROM asset_prices "
            "WHERE exchange=%s AND symbol=%s "
            "AND timestamp >= CURRENT_TIMESTAMP - make_interval(mins => 360)",
            (EXCHANGE_NAME, base))
        rng6 = ((float(row6[1]) - float(row6[0])) / float(row6[0]) * 100.0
                if row6 and row6[0] and row6[1] and float(row6[0]) > 0 else None)
        # 3h momentum
        row3 = query_one(
            "SELECT a.price / b.price - 1 FROM "
            "(SELECT price FROM asset_prices WHERE exchange=%s AND symbol=%s "
            " ORDER BY timestamp DESC LIMIT 1) a, "
            "(SELECT price FROM asset_prices WHERE exchange=%s AND symbol=%s "
            " AND timestamp <= CURRENT_TIMESTAMP - make_interval(mins => 180) "
            " ORDER BY timestamp DESC LIMIT 1) b",
            (EXCHANGE_NAME, base, EXCHANGE_NAME, base))
        mom3 = (float(row3[0]) * 100.0 if row3 and row3[0] else None)
        snapshot.append({
            "symbol": sym,
            "price": round(price, 6),
            "rng6h_pct": round(rng6, 2) if rng6 else None,
            "mom3h_pct": round(mom3, 2) if mom3 else None,
        })
    return snapshot


def read_v2_config():
    """Read current v2 constants as a dict."""
    with open(V2_SCRIPT) as f:
        src = f.read()
    config = {}
    # Simple pattern: grab top-level uppercase assignments
    for line in src.splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        # skip non-constant-looking ones
        m = re.match(r"^([A-Z][A-Z0-9_]+)\s*=\s*(.+?)(\s*#.*)?$", line)
        if not m:
            continue
        name, val_str = m.group(1), m.group(2).strip()
        val_str = re.sub(r"\s*#.*", "", val_str).strip()
        try:
            config[name] = ast.literal_eval(val_str)
        except (ValueError, SyntaxError):
            config[name] = val_str
    return config


# ---------------------------------------------------------------------------
# Apply actions from AI
# ---------------------------------------------------------------------------
def apply_parameter_change(param, value):
    """Modify a constant in v2 script."""
    with open(V2_SCRIPT) as f:
        src = f.read()
    old_line = re.search(rf"^{param}\s*=.*$", src, re.MULTILINE)
    if not old_line:
        return False, f"{param} not found"
    new_line = f"{param} = {value!r}"
    src = src[:old_line.start()] + new_line + src[old_line.end():]
    with open(V2_SCRIPT, "w") as f:
        f.write(src)
    return True, f"{param} → {value}"


def _log_trade(action, ticker, entry_price, current_price, quantity,
               estimated_value, reason):
    """Log a trade to the DB."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            from db_prices import log_trade as db_log_trade
            cur.execute(
                "INSERT INTO trade_log (exchange, action, ticker, signal_strength, "
                "entry_price, current_price, quantity, estimated_value, reason, "
                "portfolio_equity, unrealized_plpc, momentum_pct, position_size_pct) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                "(SELECT portfolio_equity FROM trade_log WHERE exchange=%s "
                " ORDER BY timestamp DESC LIMIT 1),"
                "0.0, 0.0, 0.0)",
                (EXCHANGE_NAME, action, ticker, "AI_OVERSEER",
                 entry_price, current_price, quantity, estimated_value, reason,
                 EXCHANGE_NAME))
        conn.commit()
    except Exception as e:
        print(f"_log_trade failed: {e}", file=sys.stderr)
    finally:
        conn.close()


def _save_position(symbol, price):
    """Save open position to trading_state."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM trading_state WHERE exchange=%s AND symbol=%s",
                (EXCHANGE_NAME, symbol))
            cur.execute(
                "INSERT INTO trading_state (exchange, symbol, entry_price, "
                "entry_time, peak_plpc) VALUES (%s,%s,%s,NOW(),0.0)",
                (EXCHANGE_NAME, symbol, price))
        conn.commit()
    except Exception as e:
        print(f"_save_position failed: {e}", file=sys.stderr)
    finally:
        conn.close()


def _remove_position(symbol):
    """Remove position from trading_state."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM trading_state WHERE exchange=%s AND symbol=%s",
                (EXCHANGE_NAME, symbol))
        conn.commit()
    except Exception as e:
        print(f"_remove_position failed: {e}", file=sys.stderr)
    finally:
        conn.close()


def execute_trade(action, symbol, size_eur, reason):
    """Buy or sell via Kraken market order."""
    if size_eur > MAX_TRADE_SIZE_EUR:
        size_eur = MAX_TRADE_SIZE_EUR
    # Check available balance
    try:
        balance = exchange.fetch_balance()
        available_eur = float(balance["EUR"]["free"])
    except Exception as e:
        return False, f"Balance check failed: {e}"
    try:
        exchange.load_markets()
        ticker = exchange.fetch_ticker(symbol)
        price = ticker["last"]
        if action.upper() == "BUY":
            if available_eur < size_eur:
                return False, f"Insufficient EUR: have €{available_eur:.2f}, need €{size_eur:.2f}"
            qty = size_eur / price
            fqty = float(exchange.amount_to_precision(symbol, qty))
            res = exchange.create_market_buy_order(symbol, fqty)
            # Log to DB
            _log_trade(action="BUY", ticker=symbol, entry_price=price,
                       current_price=price, quantity=fqty,
                       estimated_value=qty * price,
                       reason=f"AI overseer: {reason}")
            # Save to trading_state
            _save_position(symbol, price)
        elif action.upper() == "SELL":
            # find position qty from trading_state
            rows = query_all(
                "SELECT quantity FROM trading_state WHERE exchange=%s AND symbol=%s",
                (EXCHANGE_NAME, symbol))
            if not rows:
                rows = query_all(
                    "SELECT quantity FROM trade_log WHERE exchange=%s AND ticker=%s "
                    "AND action='BUY' ORDER BY timestamp DESC LIMIT 1",
                    (EXCHANGE_NAME, symbol))
            if not rows:
                return False, f"No position found for {symbol}"
            qty = float(rows[0][0])
            fqty = float(exchange.amount_to_precision(symbol, qty))
            res = exchange.create_market_sell_order(symbol, fqty)
            # Log to DB
            _log_trade(action="SELL", ticker=symbol, entry_price=price,
                       current_price=price, quantity=fqty,
                       estimated_value=qty * price,
                       reason=f"AI overseer: {reason}")
            # Remove from trading_state
            _remove_position(symbol)
        else:
            return False, f"Unknown action: {action}"
        return True, f"{action} {symbol} (EUR {size_eur}) — order {res.get('id')}"
    except Exception as e:
        return False, f"{action} {symbol} failed: {e}"


# ---------------------------------------------------------------------------
# AI call
# ---------------------------------------------------------------------------
def build_prompt(portfolio, positions, trades, market, config, available_eur):
    """Token-efficient prompt for the AI overseer."""

    pos_lines = []
    for p in positions:
        sym, entry, etime, peak = p
        pos_lines.append(f"  {sym}: entry={float(entry):.6f}, peak_plpc={float(peak):.2f}%")

    trade_lines = []
    for t in trades[:10]:
        ts, act, sym, ep, cp, pl, val, reason = t
        trade_lines.append(f"  {ts.strftime('%H:%M')} {act:4s} {sym:10s} "
                           f"pl={float(pl)*100 if pl else 0:+.2f}% {reason or ''}")

    market_lines = []
    for m in market:
        market_lines.append(f"  {m['symbol']:10s} price={m['price']:.6f} "
                            f"rng6h={m['rng6h_pct']!s}% mom3h={m['mom3h_pct']!s}%")

    config_lines = []
    exclude = {"CRYPTO_PAIRS", "ROUND_TRIP_FEE_PCT", "VOL_WINDOW_MIN",
               "TREND_3H_MIN", "TREND_6H_MIN", "MIN_TRADE_EUR",
               "MAX_OPEN_SMALL", "MAX_OPEN_LARGE", "EQUITY_TWO_POS",
               "COOLDOWN_MIN", "MAX_TRADES_PER_DAY", "DAILY_LOSS_BREAKER_PCT"}
    for k, v in config.items():
        if k in exclude or k.startswith("_"):
            continue
        config_lines.append(f"  {k} = {v!r}")

    prompt = f"""You are an AI trading overseer for a Kraken crypto bot. You run every hour to catch what the script misses.

CURRENT STATE
Portfolio: ~€{portfolio or '?'}  (available EUR: €{available_eur:.2f})
Open positions:
{chr(10).join(pos_lines) if pos_lines else '  (none)'}

Recent trades (last 24h):
{chr(10).join(trade_lines) if trade_lines else '  (none)'}

Market snapshot:
{chr(10).join(market_lines) if market_lines else '  (no data)'}

Current script config (v2):
{chr(10).join(config_lines) if config_lines else '  (empty)'}

YOUR JOB (respond with VALID JSON only — no markdown, no explanation):
1. ANALYSIS: 1-2 sentence summary of what's happening in the market right now.
2. PARAMETER_ADJUSTMENTS: If the script is tuned wrong, suggest new values. Bounds:
   {json.dumps(ADJUSTMENT_BOUNDS, indent=2)}
3. TRADE_SIGNALS: If you see a clear opportunity the script is missing (e.g., a regime shift, a pair not in the pool, a breakout the script's pullback logic would reject), suggest a direct trade. Max {MAX_TRADE_SIZE_EUR} EUR per trade.
4. SCRIPT_IMPROVEMENTS: Any logic changes the script needs (1 sentence max).

Return JSON:
{{
  "analysis": "...",
  "parameter_adjustments": [{{"param": "PARAM_NAME", "value": number, "reason": "..."}}],
  "trade_signals": [{{"action": "BUY|SELL", "symbol": "BTC/EUR", "size_eur": number, "reason": "..."}}],
  "script_improvements": ["..."]
}}
"""
    return prompt


def call_ai(prompt):
    client = OpenAI(api_key=DEEPSEEK_KEY, base_url=DEEPSEEK_BASE)
    resp = client.chat.completions.create(
        model=AI_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=800,
    )
    return resp.choices[0].message.content.strip()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    log = []
    log.append(f"=== AI Overseer run at {datetime.now(timezone.utc).isoformat()} ===")

    # 1. Gather state
    portfolio = get_portfolio_value()
    positions = get_open_positions()
    trades = get_recent_trades()
    market = get_market_snapshot()
    config = read_v2_config()
    # Get available EUR balance
    try:
        balance = exchange.fetch_balance()
        available_eur = float(balance["EUR"]["free"])
    except Exception as e:
        available_eur = 0.0
        log.append(f"WARN: could not fetch balance: {e}")

    log.append(f"Portfolio: ~€{portfolio or '?'}")
    log.append(f"Open positions: {len(positions)}")
    log.append(f"Recent trades: {len(trades)}")

    # 2. Build prompt & call AI
    prompt = build_prompt(portfolio, positions, trades, market, config, available_eur)
    # log full prompt for debugging
    with open(os.path.join(LOG_DIR, "last_prompt.txt"), "w") as f:
        f.write(prompt)

    try:
        reply = call_ai(prompt)
    except Exception as e:
        log.append(f"AI call failed: {e}")
        _write_log(log)
        return

    with open(os.path.join(LOG_DIR, "last_reply.txt"), "w") as f:
        f.write(reply)

    # 3. Parse JSON
    # Strip markdown fences if present
    reply_clean = re.sub(r"^```(?:json)?\s*", "", reply.strip(), flags=re.MULTILINE)
    reply_clean = re.sub(r"\s*```$", "", reply_clean.strip())
    try:
        decision = json.loads(reply_clean)
    except json.JSONDecodeError:
        log.append(f"AI reply not valid JSON: {reply[:500]}")
        _write_log(log)
        return

    log.append(f"AI analysis: {decision.get('analysis', '(none)')}")

    # 4. Apply parameter adjustments
    for adj in decision.get("parameter_adjustments", []):
        param = adj.get("param", "")
        value = adj.get("value")
        reason = adj.get("reason", "")
        bounds = ADJUSTMENT_BOUNDS.get(param)
        if bounds and (value < bounds[0] or value > bounds[1]):
            log.append(f"SKIP {param}={value} (out of bounds {bounds}) — {reason}")
            continue
        ok, msg = apply_parameter_change(param, value)
        log.append(f"{'OK' if ok else 'FAIL'} param: {msg} — {reason}")

    # 5. Execute trade signals
    for sig in decision.get("trade_signals", []):
        action = sig.get("action", "").upper()
        symbol = sig.get("symbol", "")
        size_eur = min(float(sig.get("size_eur", 10)), MAX_TRADE_SIZE_EUR)
        reason = sig.get("reason", "")
        if action not in ("BUY", "SELL"):
            log.append(f"SKIP trade: invalid action {action}")
            continue
        if symbol not in [m["symbol"] for m in market] and action == "BUY":
            log.append(f"SKIP BUY {symbol}: not in market pool")
            continue
        ok, msg = execute_trade(action, symbol, size_eur, reason)
        log.append(f"{'OK' if ok else 'FAIL'} trade: {msg} — {reason}")

    # 6. Log script improvements
    for imp in decision.get("script_improvements", []):
        log.append(f"SCRIPT IDEA: {imp}")

    _write_log(log)


def _write_log(lines):
    """Append to today's log file."""
    logfile = os.path.join(LOG_DIR, f"ai_overseer_{datetime.now().strftime('%Y-%m-%d')}.log")
    with open(logfile, "a") as f:
        for line in lines:
            ts = datetime.now().strftime("%H:%M:%S")
            f.write(f"[{ts}] {line}\n")
    # Also print for cron capture
    for line in lines:
        print(line)


if __name__ == "__main__":
    main()
