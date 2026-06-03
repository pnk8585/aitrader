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
import fcntl
import ccxt
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from openai import OpenAI

# --- Paths -----------------------------------------------------------------
PROJECT_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))
V2_SCRIPT = os.path.join(PROJECT_DIR, "traders/crypto_trades/kraken_pullback.py")
ENV_PATH = os.path.join(PROJECT_DIR, ".env")
HERMES_ENV = os.path.join(os.path.expanduser("~"), ".hermes/.env")
LOG_DIR = os.path.join(PROJECT_DIR, "logs/ai_overseer")
os.makedirs(LOG_DIR, exist_ok=True)

load_dotenv(dotenv_path=ENV_PATH)

# --- DeepSeek key (from Hermes env) ----------------------------------------
load_dotenv(dotenv_path=HERMES_ENV)
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY")
if not DEEPSEEK_KEY:
    print("FATAL: DEEPSEEK_API_KEY not found", file=sys.stderr)
    sys.exit(1)

AI_MODEL = "deepseek-v4-flash"  # explicit V4 Flash via api.deepseek.com
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
GATES_FILE = os.path.join(PROJECT_DIR, "ai_overseer/ai_gate.json")
MAX_TRADE_SIZE_EUR = 30.0         # max EUR per AI-triggered trade
MAX_POSITIONS_AI = 1               # max positions opened by AI
ADJUSTMENT_BOUNDS = {
    "VOL_FLOOR_PCT": (1.5, 8.0),
    "PULLBACK_MIN_PCT": (0.2, 3.0),
    "BLOWOFF_GUARD_1H_PCT": (2.0, 8.0),
    "MIN_HARD_STOP_PCT": (1.0, 5.0),
    "TRAIL_ARM_PCT": (0.5, 3.0),
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


def _atomic_write(path, content):
    """Write a file atomically (temp + os.replace).

    The 5-min v2 cron both reads ai_gate.json and imports the v2 script every
    cycle. A plain truncate-then-write leaves a window where a concurrent reader
    sees a half-written / empty file. os.replace is atomic on POSIX, so readers
    only ever see the old or the new file — never a torn one.
    """
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Gather state
# ---------------------------------------------------------------------------
def get_portfolio_value():
    """Latest portfolio_equity from trade_log (any kraken strategy)."""
    row = query_one("SELECT portfolio_equity FROM trade_log "
                     "WHERE exchange LIKE %s ORDER BY timestamp DESC LIMIT 1",
                     ("kraken%",))
    return float(row[0]) if row else None


def get_open_positions():
    """Read current positions from trading_state (all kraken strategies)."""
    rows = query_all(
        "SELECT symbol, entry_price, entry_time, peak_plpc "
        "FROM trading_state WHERE exchange LIKE %s", ("kraken%",))
    return rows


def get_recent_trades(limit=15):
    """Last N trades with details (all kraken strategies)."""
    rows = query_all(
        "SELECT timestamp, action, ticker, entry_price, current_price, "
        "       unrealized_plpc, estimated_value, reason "
        "FROM trade_log WHERE exchange LIKE %s "
        "ORDER BY timestamp DESC LIMIT %s",
        ("kraken%", limit))
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
    """Modify a tunable constant in the v2 script — safely.

    Only constants in ADJUSTMENT_BOUNDS are editable, and only numeric values.
    The rewritten source is parsed with ast BEFORE it is swapped in, and the
    swap is atomic, so a malformed edit can never crash the 5-min cron that is
    importing this file.
    """
    if param not in ADJUSTMENT_BOUNDS:
        return False, f"{param} is not an adjustable parameter"
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False, f"{param} value must be numeric (got {value!r})"
    with open(V2_SCRIPT) as f:
        src = f.read()
    # Match only a top-level assignment, preserving any trailing comment.
    pat = re.compile(rf"^{re.escape(param)}\s*=\s*[^\n#]*(\s*#.*)?$", re.MULTILINE)
    m = pat.search(src)
    if not m:
        return False, f"{param} not found"
    trailing = m.group(1) or ""
    new_line = f"{param} = {value!r}{trailing}"
    new_src = src[:m.start()] + new_line + src[m.end():]
    try:
        ast.parse(new_src)
    except SyntaxError as e:
        return False, f"{param} edit would break v2 syntax: {e}"
    _atomic_write(V2_SCRIPT, new_src)
    return True, f"{param} → {value}"


def _log_trade(action, ticker, entry_price, current_price, quantity,
               estimated_value, reason):
    """Log a trade to the DB."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
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


def _save_position(symbol, price, quantity=0.0):
    """Upsert an open position into trading_state (incl. quantity)."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO trading_state (exchange, symbol, entry_price, "
                "entry_time, peak_plpc, quantity) VALUES (%s,%s,%s,NOW(),0.0,%s) "
                "ON CONFLICT (exchange, symbol) DO UPDATE SET "
                "entry_price=EXCLUDED.entry_price, entry_time=NOW(), "
                "peak_plpc=0.0, quantity=EXCLUDED.quantity",
                (EXCHANGE_NAME, symbol, price, quantity))
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


def _extract_fill(res, fallback_price):
    """Recover actual average fill price + filled qty from a CCXT order."""
    price, qty = None, None
    if isinstance(res, dict):
        for k in ("average", "price"):
            v = res.get(k)
            if v:
                price = float(v)
                break
        filled = res.get("filled")
        cost = res.get("cost")
        if filled:
            qty = float(filled)
        if price is None and cost and filled:
            price = float(cost) / float(filled)
    return (price or fallback_price), qty


def execute_trade(action, symbol, size_eur, reason):
    """Buy or sell via Kraken market order."""
    action = action.upper()
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
        if action == "BUY":
            if available_eur < size_eur:
                return False, f"Insufficient EUR: have €{available_eur:.2f}, need €{size_eur:.2f}"
            qty = size_eur / price
            # Pre-flight min lot check (Kraken rejects orders below minimum)
            mkt = exchange.market(symbol)
            min_amt = mkt['limits']['amount']['min']
            if min_amt and qty < min_amt:
                return False, (f"Order qty ({qty:.4f} {mkt['base']}) below exchange minimum "
                               f"({min_amt} {mkt['base']})")
            fqty = float(exchange.amount_to_precision(symbol, qty))
            res = exchange.create_market_buy_order(symbol, fqty)
            # Use real fill, not the pre-trade ticker — market orders slip.
            fill_price, fill_qty = _extract_fill(res, price)
            if fill_qty is None:
                fill_qty = fqty
            _log_trade(action="BUY", ticker=symbol, entry_price=fill_price,
                       current_price=fill_price, quantity=fill_qty,
                       estimated_value=fill_qty * fill_price,
                       reason=f"AI overseer: {reason}")
            _save_position(symbol, fill_price, fill_qty)
        elif action == "SELL":
            # Source of truth for qty is the actual on-exchange balance, then the
            # stored quantity, then the last BUY in trade_log.
            base = symbol.split("/")[0]
            qty = float(balance.get(base, {}).get("free", 0.0) or 0.0)
            if qty <= 0:
                rows = query_all(
                    "SELECT quantity FROM trading_state WHERE exchange=%s AND symbol=%s",
                    (EXCHANGE_NAME, symbol))
                if not rows or not rows[0][0]:
                    rows = query_all(
                        "SELECT quantity FROM trade_log WHERE exchange=%s AND ticker=%s "
                        "AND action='BUY' ORDER BY timestamp DESC LIMIT 1",
                        (EXCHANGE_NAME, symbol))
                if not rows or not rows[0][0]:
                    return False, f"No position found for {symbol}"
                qty = float(rows[0][0])
            fqty = float(exchange.amount_to_precision(symbol, qty))
            res = exchange.create_market_sell_order(symbol, fqty)
            fill_price, fill_qty = _extract_fill(res, price)
            if fill_qty is None:
                fill_qty = fqty
            _log_trade(action="SELL", ticker=symbol, entry_price=fill_price,
                       current_price=fill_price, quantity=fill_qty,
                       estimated_value=fill_qty * fill_price,
                       reason=f"AI overseer: {reason}")
            _remove_position(symbol)
        else:
            return False, f"Unknown action: {action}"
        return True, f"{action} {symbol} (EUR {size_eur}) — order {res.get('id')}"
    except Exception as e:
        return False, f"{action} {symbol} failed: {e}"


def write_gates(gates, log):
    """Save AI gate conditions to file for the 5-min script to read."""
    os.makedirs(os.path.dirname(GATES_FILE), exist_ok=True)
    payload = {
        "script_paused": gates.get("script_paused", False),
        "consult_on_entry": gates.get("consult_on_entry", False),
        "reason": gates.get("reason"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write(GATES_FILE, json.dumps(payload, indent=2))
    log.append(f"GATES: paused={payload['script_paused']} consult={payload['consult_on_entry']} — {payload.get('reason', 'no reason')}")


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

YOUR JOB (respond with ONLY valid JSON — no markdown, no backticks, no explanation):
1. ANALYSIS: 1-2 sentence summary.
2. PARAMETER_ADJUSTMENTS: If the script is tuned wrong, suggest new values. Bounds:
   {json.dumps(ADJUSTMENT_BOUNDS, indent=2)}
3. TRADE_SIGNALS: **You MUST submit a BUY signal when you see a strong setup.** Available EUR is €{available_eur:.2f}. Look for: NEAR/RENDER with strong 3h momentum (>+1%) and high vol (>5%), or any pair with momentum >+2%. If no pair qualifies, leave empty. Max {MAX_TRADE_SIZE_EUR} EUR per trade.
   **ROTATION:** If you hold a position (e.g. NEAR) but another pair has clearly better momentum (e.g. RENDER >+3% vs your position <+1%), submit TWO signals in the same array: SELL the current position, BUY the better one. The SELL size_eur is ignored (sells full position).
4. GATES: Set conditions for the 5-min script. Use these sparingly — only when market regime demands it:
   - script_paused: true/false
   - consult_on_entry: true/false
   - reason: short explanation
5. SCRIPT_IMPROVEMENTS: Any logic changes the script needs (1 sentence max).

Return strict JSON — no comments, no trailing commas:
{{{{
  "analysis": "...",
  "parameter_adjustments": [{{"param": "PARAM_NAME", "value": number, "reason": "..."}}],
  "trade_signals": [{{"action": "BUY", "symbol": "NEAR/EUR", "size_eur": {MAX_TRADE_SIZE_EUR}, "reason": "..."}}],
  "gates": {{ "script_paused": false, "consult_on_entry": false, "reason": null }},
  "script_improvements": ["..."]
}}}}
IMPORTANT: If NEAR/EUR or RENDER/EUR has 3h momentum > +1% AND 6h range > 5% AND you have available EUR (>€5), you SHOULD submit a BUY signal. If available EUR is low but you hold a position with weak momentum and another pair has >+2% stronger momentum, SELL the weak position and BUY the strong one.
"""
    return prompt


def _build_review_prompt(pending, ctx, portfolio, available_eur, num_positions, recent_pnl):
    """Build a focused prompt to evaluate ONE candidate trade setup via self-debate."""
    sym = pending.get("symbol", "?")
    bot = pending.get("bot", "?")
    price = pending.get("price", "?")
    score = pending.get("score", "?")
    signals = pending.get("signals", {})
    desc = pending.get("momentum_desc", "N/A")
    strategy = pending.get("strategy", bot)

    pnl_lines = []
    wins = losses = 0
    for t in recent_pnl[:10]:
        ts, tk, pl, r = t
        plf = float(pl) if pl else 0.0
        if plf > 0:
            wins += 1
        elif plf < 0:
            losses += 1
        pnl_lines.append(f"  {ts.strftime('%H:%M')} {tk:10s} pl={plf*100:+.2f}% {r or ''}")
    pnl_block = chr(10).join(pnl_lines) if pnl_lines else "  (no recent sells)"
    track = f"{wins}W/{losses}L last {wins + losses}" if (wins + losses) else "no recent history"

    prompt = f"""You are a disciplined crypto risk officer deciding whether to BUY {sym} on Kraken RIGHT NOW.

CANDIDATE
  Symbol: {sym}  Price: €{price}  Strategy: {strategy} ({bot})
  Setup: {desc}
  Quality score: {score}
  Signals: {json.dumps(signals)}

MARKET ({sym})
  6h range: {ctx.get('rng6h_pct', 'N/A')}%   3h momentum: {ctx.get('mom3h_pct', 'N/A')}%

PORTFOLIO
  Equity ~€{portfolio or '?'}  Available EUR: €{available_eur:.2f}  Open positions: {num_positions}

THIS BOT'S RECENT TRACK RECORD ({track}):
{pnl_block}

Debate this internally before answering. Do NOT show the debate.
1. BULL CASE: What supports buying NOW? Momentum direction, setup quality ({score}), trend structure, room to run in the 6h range.
2. BEAR CASE: What argues against? Recent losses in the track record, choppy/overextended price, weak score, thin macro, too little EUR (€{available_eur:.2f}) for a meaningful size.
3. RISK ASSESSMENT: Weigh both sides. Is reward worth the downside? If the bot is bleeding ({track}), demand a stronger edge. If EUR can't fund a real position, lean REJECT.
4. DECISION: Pick the side with stronger evidence.

Then respond with VALID JSON only:
{{
  "verdict": "APPROVE or REJECT",
  "reason": "Short explanation (max 100 chars)",
  "confidence": 5
}}
Confidence 1-10. 1 = very unsure, 10 = extremely confident."""
    return prompt


def _extract_json(text):
    """Extract JSON from AI response — handles markdown fences, leading/trailing text."""
    # Strip markdown fences
    text = re.sub(r"^```(?:json)?\s*\n?", "", text.strip(), flags=re.MULTILINE)
    text = re.sub(r"\s*```\s*$", "", text.strip())
    # Find first { and last }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end+1]
    return text


def call_ai(prompt):
    client = OpenAI(api_key=DEEPSEEK_KEY, base_url=DEEPSEEK_BASE)
    resp = client.chat.completions.create(
        model=AI_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=800,
        response_format={"type": "json_object"},
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

    # ---------------------------------------------------------------
    # 2a. Handle pending per-trade review (bot → AI → approve/reject)
    # ---------------------------------------------------------------
    PENDING_REVIEW_FILE = os.path.join(PROJECT_DIR, "ai_overseer/pending_review.json")
    if os.path.exists(PENDING_REVIEW_FILE):
        try:
            with open(PENDING_REVIEW_FILE) as f:
                pending = json.load(f)
        except (json.JSONDecodeError, IOError):
            pending = {}

        if pending.get("status") == "pending":
            sym = pending.get("symbol", "?")
            bot = pending.get("bot", "?")
            price = pending.get("price")
            score = pending.get("score")
            signals = pending.get("signals", {})
            desc = pending.get("momentum_desc", "")
            log.append(f"Processing pending review: {sym} ({bot}, score {score})")
            # Capture candidate_id to detect overwrites during AI call
            original_candidate_id = pending.get("candidate_id")

            # Gather market context for this symbol only
            ctx = {"symbol": sym, "price": price}
            base = sym.split("/")[0] if "/" in sym else sym
            row6 = query_one(
                "SELECT MIN(price), MAX(price) FROM asset_prices "
                "WHERE exchange=%s AND symbol=%s "
                "AND timestamp >= CURRENT_TIMESTAMP - make_interval(mins => 360)",
                (EXCHANGE_NAME, base))
            rng6 = ((float(row6[1]) - float(row6[0])) / float(row6[0]) * 100.0
                    if row6 and row6[0] and row6[1] and float(row6[0]) > 0 else None)
            row3 = query_one(
                "SELECT a.price / b.price - 1 FROM "
                "(SELECT price FROM asset_prices WHERE exchange=%s AND symbol=%s "
                " ORDER BY timestamp DESC LIMIT 1) a, "
                "(SELECT price FROM asset_prices WHERE exchange=%s AND symbol=%s "
                " AND timestamp <= CURRENT_TIMESTAMP - make_interval(mins => 180) "
                " ORDER BY timestamp DESC LIMIT 1) b",
                (EXCHANGE_NAME, base, EXCHANGE_NAME, base))
            mom3 = (float(row3[0]) * 100.0 if row3 and row3[0] else None)
            ctx["rng6h_pct"] = round(rng6, 2) if rng6 else "N/A"
            ctx["mom3h_pct"] = round(mom3, 2) if mom3 else "N/A"

            # Recent PnL for this bot
            recent_pnl = query_all(
                "SELECT timestamp, ticker, unrealized_plpc, reason FROM trade_log "
                "WHERE exchange=%s AND action='SELL' "
                "ORDER BY timestamp DESC LIMIT 20",
                (bot,))

            review_prompt = _build_review_prompt(pending, ctx, portfolio, available_eur,
                                                  len(positions), recent_pnl)
            try:
                reply = call_ai(review_prompt)
                log.append(f"Review AI reply received for {sym}")
            except Exception as e:
                log.append(f"Review AI call failed for {sym}: {e}")
                with open(os.path.join(LOG_DIR, "last_review_reply.txt"), "w") as f:
                    f.write(f"ERROR: {e}")
                # Leave pending as-is for next retry
                _write_log(log)
                # Continue to main AI call below — don't block the overseer
                pending = {}

            if reply:
                reply_clean = _extract_json(reply)
                with open(os.path.join(LOG_DIR, "last_review_reply.txt"), "w") as f:
                    f.write(reply_clean)
                try:
                    decision = json.loads(reply_clean)
                    verdict = decision.get("verdict", "REJECT")
                    reason = decision.get("reason", "")
                    confidence = decision.get("confidence", 5)

                    # Normalize: bots check for "approved"/"rejected" (with 'd')
                    # while the prompt asks the AI for "APPROVE"/"REJECT"
                    v = verdict.strip().upper()
                    if v.startswith("APPROV"):
                        pending["status"] = "approved"
                        pending["verdict"] = "APPROVE"
                    elif v.startswith("REJECT"):
                        pending["status"] = "rejected"
                        pending["verdict"] = "REJECT"
                    else:
                        pending["status"] = "rejected"
                        pending["verdict"] = "REJECT"
                        # Also propagate unknown verdict as the reason
                        reason = f"Unknown verdict '{verdict}': {reason}" if reason else f"Unknown verdict '{verdict}'"
                    pending["verdict_reason"] = reason
                    pending["confidence"] = confidence
                    PENDING_LOCK_FILE = os.path.join(PROJECT_DIR, "ai_overseer/.pending_review.lock")
                    os.makedirs(os.path.dirname(PENDING_LOCK_FILE), exist_ok=True)
                    pending["reviewed_at"] = datetime.now(timezone.utc).isoformat()

                    # Re-read file under flock: if candidate_id changed, discard
                    verdict_written = False
                    try:
                        with open(PENDING_LOCK_FILE, "w") as lf, \
                             open(PENDING_REVIEW_FILE) as f_check:
                            fcntl.flock(lf, fcntl.LOCK_EX)
                            current = json.load(f_check)
                            if current.get("candidate_id") != original_candidate_id:
                                log.append(f"⚠️ Discarding verdict for {sym}: candidate changed "
                                           f"(new candidate_id) during AI call")
                                pending = {}
                                verdict = None
                            else:
                                tmp = f"{PENDING_REVIEW_FILE}.tmp.{os.getpid()}"
                                with open(tmp, "w") as f:
                                    json.dump(pending, f, indent=2, default=str)
                                    f.flush()
                                    os.fsync(f.fileno())
                                os.replace(tmp, PENDING_REVIEW_FILE)
                                verdict_written = True
                    except FileNotFoundError:
                        log.append(f"⚠️ Discarding verdict for {sym}: pending file gone")
                        pending = {}
                        verdict = None
                    except Exception:
                        tmp = f"{PENDING_REVIEW_FILE}.tmp.{os.getpid()}"
                        with open(tmp, "w") as f:
                            json.dump(pending, f, indent=2, default=str)
                            f.flush()
                            os.fsync(f.fileno())
                        os.replace(tmp, PENDING_REVIEW_FILE)
                        verdict_written = True

                    if not verdict_written and pending:
                        tmp = f"{PENDING_REVIEW_FILE}.tmp.{os.getpid()}"
                        with open(tmp, "w") as f:
                            json.dump(pending, f, indent=2, default=str)
                            f.flush()
                            os.fsync(f.fileno())
                        os.replace(tmp, PENDING_REVIEW_FILE)

                    if verdict is not None:
                        log.append(f"✅ Review verdict for {sym}: {verdict} — {reason}")
                        emoji = "✅" if verdict == "APPROVE" else "❌"
                        print(f"🤖 {emoji} {bot}: {verdict} {sym} — {reason}")
                except (json.JSONDecodeError, KeyError) as e:
                    log.append(f"Review verdict parse error for {sym}: {e}")
                    # Don't leave pending — reject on parse failure
                    pending["status"] = "rejected"
                    pending["verdict"] = "REJECT"
                    pending["verdict_reason"] = f"AI reply parse error: {e}"
                    pending["reviewed_at"] = datetime.now(timezone.utc).isoformat()
                    # Re-read file under flock: don't overwrite if candidate changed during AI call
                    try:
                        with open(PENDING_LOCK_FILE, "w") as lf, \
                             open(PENDING_REVIEW_FILE) as f_check:
                            fcntl.flock(lf, fcntl.LOCK_EX)
                            current = json.load(f_check)
                            if current.get("candidate_id") != original_candidate_id:
                                log.append(f"⚠️ Not writing parse-error rejection for {sym}: "
                                           f"candidate changed during AI call")
                                pending = {}
                            else:
                                tmp = f"{PENDING_REVIEW_FILE}.tmp.{os.getpid()}"
                                with open(tmp, "w") as f:
                                    json.dump(pending, f, indent=2, default=str)
                                    f.flush()
                                    os.fsync(f.fileno())
                                os.replace(tmp, PENDING_REVIEW_FILE)
                    except FileNotFoundError:
                        pending = {}
                    except Exception:
                        tmp = f"{PENDING_REVIEW_FILE}.tmp.{os.getpid()}"
                        with open(tmp, "w") as f:
                            json.dump(pending, f, indent=2, default=str)
                            f.flush()
                            os.fsync(f.fileno())
                        os.replace(tmp, PENDING_REVIEW_FILE)

    # 2b. Build prompt & call AI
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

    # 3. Parse JSON — retry once if invalid
    reply_clean = _extract_json(reply)
    try:
        decision = json.loads(reply_clean)
    except json.JSONDecodeError:
        log.append(f"AI reply not valid JSON (1st attempt): {reply[:300]}...")
        # Retry: ask again with stricter instruction
        try:
            retry_prompt = prompt + "\n\n⚠️ Your previous response was NOT valid JSON. Return ONLY raw JSON with no backticks, no markdown, no extra text. Start with { and end with }."
            reply2 = call_ai(retry_prompt)
            reply2_clean = _extract_json(reply2)
            decision = json.loads(reply2_clean)
            log.append("AI replied valid JSON on retry")
        except (json.JSONDecodeError, Exception) as e2:
            log.append(f"AI reply not valid JSON (retry failed): {e2}")
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

    # 5.5. Process gates
    gates = decision.get("gates", {})
    if gates:
        write_gates(gates, log)

    # 6. Log script improvements
    for imp in decision.get("script_improvements", []):
        log.append(f"SCRIPT IDEA: {imp}")

    _write_log(log)

    # Heartbeat: account status + notify state
    try:
        bal_local = exchange.fetch_balance()
        cash_e = float(bal_local["EUR"]["free"])
        pos_parts = []
        for m in market:
            coin = m["symbol"].split("/")[0]
            qty = float(bal_local["total"].get(coin, 0))
            if qty > 1e-8 and m.get("price"):
                val = qty * m["price"]
                if val > 1.0:
                    pos_parts.append(f"{coin} {round(val,2)}€")
        pos_str = " · ".join(pos_parts) if pos_parts else "κανένα"
        print(f"🤖 AI Overseer: {round(cash_e,2)}€ free · {pos_str}")

        # Save notify_state to DB so system_health_check sees heartbeat
        db2 = get_db()
        with db2.cursor() as cur:
            cur.execute(
                """INSERT INTO notify_state (exchange, last_notify_time, extra, updated_at)
                   VALUES (%s, %s, '{}'::jsonb, CURRENT_TIMESTAMP)
                   ON CONFLICT (exchange) DO UPDATE SET
                       updated_at = CURRENT_TIMESTAMP""",
                (EXCHANGE_NAME, datetime.now(timezone.utc).isoformat()))
        db2.commit()
        db2.close()
    except Exception:
        pass


def _write_log(lines):
    """Append to today's log file (all lines written to file). Print only key findings."""
    logfile = os.path.join(LOG_DIR, f"ai_overseer_{datetime.now().strftime('%Y-%m-%d')}.log")
    with open(logfile, "a") as f:
        for line in lines:
            ts = datetime.now().strftime("%H:%M:%S")
            f.write(f"[{ts}] {line}\n")
    # Print only the meaningful lines for Telegram
    for line in lines:
        if line.startswith("OK trade") or line.startswith("FAIL trade"):
            emoji = "✅" if line.startswith("OK") else "❌"
            # Extract action and symbol: "OK trade: Bought ETH for EUR 12 @ 3842 — reason"
            print(f"🤖 {emoji} {line}")
        elif line.startswith("GATES:"):
            print(f"🤖 🚪 {line}")
        elif line.startswith("AI analysis:"):
            txt = line.replace("AI analysis: ", "")
            if len(txt) > 200:
                txt = txt[:200] + "…"
            print(f"🤖 {txt}")
        elif line.startswith("SCRIPT IDEA:"):
            print(f"🤖 💡 {line}")


if __name__ == "__main__":
    main()
