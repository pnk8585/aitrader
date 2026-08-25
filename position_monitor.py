#!/usr/bin/env python3
"""Position Monitor — checks open positions every 2h, LLM decides SELL/HOLD.

Replaces the old AI Overseer's buy-side logic. Only handles exits.
Triggered by the orchestrator, runs synchronously.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone

import ccxt
from dotenv import load_dotenv
from openai import OpenAI

from app.llm_prompts import get_prompt
from traders.common.exchange import market_sell
from traders.extreme.db_prices import log_successful_sell_once
from traders.common.gates import check_and_set_btc_pause
from traders.common.llm_review import _extract_json_object, _message_text
from traders.common.pnl_notify import format_sell_pnl

# ── Env ─────────────────────────────────────────────────────────────
for p in ["/home/pank/projects/aitrader/.env", "/home/pank/.hermes/.env"]:
    if os.path.exists(p):
        load_dotenv(p, override=False)

KRAKEN_KEY = os.getenv("KRAKEN_API_KEY")
KRAKEN_SECRET = os.getenv("KRAKEN_SECRET")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY")


def _resolve_llm() -> tuple[str, str, str | None]:
    """DB/settings first (host.docker.internal in container), then env/defaults."""
    model = os.getenv("AI_MODEL") or "hermes-flash"
    base_url = os.getenv("LITELLM_BASE_URL") or "http://localhost:4000/v1"
    api_key = DEEPSEEK_KEY
    try:
        from app.settings import get_ai_config
        cfg = get_ai_config()
        model = cfg.get("model") or model
        base_url = cfg.get("base_url") or base_url
        api_key = cfg.get("api_key") or api_key
    except Exception:
        pass
    return model, base_url, api_key

_PAPER_MODE = os.environ.get("AITRADER_MODE", "") == "paper"
EXCHANGE_NAME = _PAPER_MODE and "paper-position-monitor" or "position-monitor"

# trading_state rows are written by the strategies under their own keys
# "kraken" is also used (direct positions, e.g. manual buys via the dashboard)
_LIVE_STATE_EXCHANGES = ("kraken-momentum", "kraken-pullback", "kraken-high-risk", "kraken")
_PAPER_STATE_EXCHANGES = ("paper-kraken-momentum", "paper-kraken-pullback", "paper-kraken-high-risk")


def paper_state_exchanges():
    """Paper monitor keys, kept separate from any live/manual state row."""
    return _PAPER_STATE_EXCHANGES


def monitored_state_exchanges():
    """Return the current mode's only permitted trading_state keys."""
    return _PAPER_STATE_EXCHANGES if _PAPER_MODE else _LIVE_STATE_EXCHANGES


_STATE_EXCHANGES = monitored_state_exchanges()


def _close_executed_sell(db, state_exchange, symbol, *, entry_price, price_hint,
                         quantity, order, reason):
    """Record an executed SELL, then always remove its sellable state.

    Telemetry is important but cannot turn a confirmed exchange execution into
    a retryable position: that would permit a duplicate live SELL next cycle.
    """
    if _PAPER_MODE and state_exchange not in _PAPER_STATE_EXCHANGES:
        raise ValueError("paper monitor refuses non-paper state key")
    if not _PAPER_MODE and state_exchange not in _LIVE_STATE_EXCHANGES:
        raise ValueError("live monitor refuses unknown state key")
    logged = log_successful_sell_once(
        db, state_exchange, ticker=symbol, entry_price=entry_price,
        price_hint=price_hint, quantity=quantity, order=order, reason=reason)
    if not logged:
        print("CRITICAL: confirmed SELL telemetry failed; closing state to prevent duplicate SELL", file=sys.stderr)
    cur = db.cursor()
    cur.execute("DELETE FROM trading_state WHERE exchange=%s AND symbol=%s", (state_exchange, symbol))
    db.commit()
    return logged


def _sell_position_once(db, kraken, pos, reason):
    """Execute one confirmed exit and make its state non-sellable immediately."""
    symbol = pos["symbol"]
    quantity = float(kraken.amount_to_precision(symbol, pos["qty"]))
    order = market_sell(kraken, symbol, quantity, pos["current"])
    logged = _close_executed_sell(
        db, pos["state_exchange"], symbol, entry_price=pos["entry"],
        price_hint=pos["current"], quantity=quantity, order=order, reason=reason)
    return quantity, logged

# ── DB helpers ──────────────────────────────────────────────────────
def _get_db():
    import psycopg2
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", 5432)),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        connect_timeout=5,
    )


# ── LLM ─────────────────────────────────────────────────────────────
def _llm_decide(position: dict, price_ctx: str) -> dict:
    """Ask LLM: SELL or HOLD this position?"""
    model, base_url, api_key = _resolve_llm()
    if not api_key:
        return {"action": "HOLD", "reason": "no API key", "confidence": 0}
    client = OpenAI(api_key=api_key, base_url=base_url)

    system_prompt = get_prompt("position_monitor_system")
    prompt = get_prompt("position_monitor_user").format(
        symbol=position["symbol"],
        entry=position["entry"],
        current=position["current"],
        pnl_pct=position["pnl_pct"],
        qty=position["qty"],
        value=position["value"],
        exchange=position["exchange"],
        price_ctx=price_ctx,
    )

    t0 = time.monotonic()
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=400,
            timeout=15,
        )
        latency_ms = (time.monotonic() - t0) * 1000
        # Same DeepSeek/JSON extract path as llm_review.py (bettips-ai style).
        msg = resp.choices[0].message
        raw_full = _message_text(msg)
        extracted = _extract_json_object(raw_full)
        if extracted is None:
            result = {"action": "HOLD", "reason": "LLM returned text", "confidence": 5}
        else:
            try:
                result = json.loads(extracted)
            except json.JSONDecodeError:
                result = {"action": "HOLD", "reason": f"LLM parse error: {extracted[:80]}", "confidence": 5}
        action = str(result.get("action", "HOLD")).strip().upper()
        if action not in ("SELL", "HOLD"):
            action = "HOLD"
        try:
            conf = int(result.get("confidence", 5))
        except (TypeError, ValueError):
            conf = 5
        final = {
            "action": action,
            "reason": str(result.get("reason", ""))[:200],
            "confidence": max(1, min(10, conf)),
        }
        _log_position_llm(
            model=model,
            system_prompt=system_prompt,
            user_prompt=prompt,
            response=raw_full,
            status="ok",
            latency_ms=latency_ms,
            position=position,
            final=final,
        )
        return final
    except Exception as e:
        latency_ms = (time.monotonic() - t0) * 1000
        final = {"action": "HOLD", "reason": f"LLM error: {e}", "confidence": 0}
        _log_position_llm(
            model=model,
            system_prompt=system_prompt,
            user_prompt=prompt,
            response=None,
            status="error",
            error=str(e),
            latency_ms=latency_ms,
            position=position,
            final=final,
        )
        return final


def _log_position_llm(
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    response: str | None,
    status: str,
    latency_ms: float,
    position: dict,
    final: dict,
    error: str | None = None,
) -> None:
    """Structured llm.jsonl line for exit decisions. Never raises."""
    try:
        from app.logging_setup import log_llm_call
        log_llm_call(
            kind="position_monitor",
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response=response,
            status=status,
            error=error,
            latency_ms=latency_ms,
            symbol=position.get("symbol"),
            strategy="position-monitor",
            extra={
                "action": final.get("action"),
                "reason": final.get("reason"),
                "confidence": final.get("confidence"),
                "entry": position.get("entry"),
                "current": position.get("current"),
                "pnl_pct": position.get("pnl_pct"),
                "exchange": position.get("exchange"),
            },
        )
    except Exception:
        pass


# ── Price context ───────────────────────────────────────────────────
def _price_context(db, symbol: str, exchange_name: str) -> str:
    """Query recent price history for the position."""
    cur = db.cursor()
    base = symbol.split("/")[0]
    lines = []

    # Last price from DB
    cur.execute(
        """SELECT price FROM asset_prices
           WHERE exchange=%s AND symbol=%s
           ORDER BY timestamp DESC LIMIT 1""",
        (exchange_name, base))
    row = cur.fetchone()
    current = float(row[0]) if row else 0

    for label, minutes in [("1h", 60), ("6h", 360), ("24h", 1440)]:
        cur.execute(
            """SELECT price FROM asset_prices
               WHERE exchange=%s AND symbol=%s
               AND timestamp >= NOW() - (%s * INTERVAL '1 minute')
               ORDER BY timestamp LIMIT 1""",
            (exchange_name, base, minutes))
        row = cur.fetchone()
        if row:
            old = float(row[0])
            chg = (current - old) / old * 100 if old else 0
            lines.append(f"  {label} ago: €{old:.4f} → €{current:.4f} ({chg:+.2f}%)")
        else:
            lines.append(f"  {label} ago: no data")

    # BTC for crypto
    if exchange_name == "kraken" and base != "BTC":
        cur.execute(
            """SELECT price FROM asset_prices
               WHERE exchange='kraken' AND symbol='BTC'
               ORDER BY timestamp DESC LIMIT 72""")
        rows = cur.fetchall()
        if rows:
            btc_now = float(rows[0][0])
            btc_avg = sum(float(r[0]) for r in rows) / len(rows)
            lines.append(f"  BTC: €{btc_now:,.0f} ({(btc_now-btc_avg)/btc_avg*100:+.1f}% vs 6h avg)")

    return "\n".join(lines) if lines else "(no data)"


# ── Main ────────────────────────────────────────────────────────────
def main():
    # Run tracking is owned by app.cron_orchestrator (cron_runs table).
    # aitrader_registry was removed when Docker took over scheduling.
    script_name = "position-monitor"

    db = _get_db()
    # Re-arm the BTC-drawdown safety pause. ai_overseer.py used to do this
    # every cycle; it was deleted, so the pause was never being set again.
    # Idempotent + None-safe (see gates.check_and_set_btc_pause contract).
    check_and_set_btc_pause(db)

    kraken = ccxt.kraken({
        "apiKey": KRAKEN_KEY, "secret": KRAKEN_SECRET,
        "enableRateLimit": True,
    })

    log = []
    now_str = datetime.now(timezone.utc).isoformat()

    # ── Collect positions ─────────────────────────────────────────
    positions = []

    # Grid positions — grid manages its own exits, skip here.
    cur = db.cursor()
    cur.execute("SELECT symbol FROM grid_state")
    _grid_symbols = {row[0] for row in cur.fetchall()}

    # Paper mode intentionally projects positions only from paper state.  It
    # must not read the live private wallet merely to monitor simulations.
    if _PAPER_MODE:
        try:
            cur = db.cursor()
            cur.execute(
                "SELECT exchange, symbol, entry_price, quantity FROM trading_state "
                "WHERE exchange IN %s", (monitored_state_exchanges(),))
            state_rows = cur.fetchall()
            for state_exchange, sym, entry_price, quantity in state_rows:
                qty_f = float(quantity or 0.0)
                if qty_f <= 0:
                    continue
                try:
                    price = float((kraken.fetch_ticker(sym) or {}).get("last") or 0.0)
                except Exception:
                    continue
                if price <= 0 or qty_f * price < 1.0:
                    continue
                entry = float(entry_price or price)
                positions.append({
                    "exchange": "kraken", "symbol": sym, "state_exchange": state_exchange,
                    "entry": entry, "current": price, "qty": qty_f,
                    "value": qty_f * price,
                    "pnl_pct": (price - entry) / entry * 100 if entry else 0,
                })
        except Exception as e:
            log.append(f"Paper state error: {e}")
    else:
        try:
            bal = kraken.fetch_balance()
            for coin, qty in bal.get("total", {}).items():
                if coin in ("EUR", "USD", "USDT", "USDC") or float(qty) <= 0:
                    continue
                sym = f"{coin}/EUR"
                if sym in _grid_symbols:
                    continue
                qty_f = float(qty)
                try:
                    ticker = kraken.fetch_ticker(sym)
                    price = ticker.get("last", 0)
                except Exception:
                    continue
                value = qty_f * price
                if value < 1.0:  # skip sub-€1 dust
                    continue
                # Get entry from trading_state
                cur = db.cursor()
                cur.execute(
                    "SELECT exchange, entry_price FROM trading_state WHERE exchange IN %s AND symbol=%s",
                    (monitored_state_exchanges(), sym))
                row = cur.fetchone()
                state_exchange = row[0] if row else EXCHANGE_NAME
                entry = float(row[1]) if row else price
                pnl = (price - entry) / entry * 100 if entry else 0
                positions.append({
                    "exchange": "kraken", "symbol": sym,
                    "state_exchange": state_exchange,
                    "entry": entry, "current": price, "qty": qty_f,
                    "value": value, "pnl_pct": pnl,
                })
        except Exception as e:
            log.append(f"Kraken balance error: {e}")

    if not positions:
        # Empty stdout = silent Telegram (cron_orchestrator only notifies on summary).
        # Print only errors if any — never a "silent" heartbeat line.
        for line in log:
            print(line)
        db.close()
        return

    log.append(f"{now_str} | {len(positions)} positions found")

    # ── Evaluate each position ────────────────────────────────────
    for pos in positions:
        sym = pos["symbol"]
        ex = pos["exchange"]
        pnl = pos["pnl_pct"]
        log.append(f"  {sym}: entry €{pos['entry']:.4f} now €{pos['current']:.4f} ({pnl:+.2f}%)")

        # Hard stop at -15% — sell without LLM
        if pnl <= -15:
            log.append(f"    🛑 HARD STOP -15% — selling immediately")
            if ex == "kraken":
                try:
                    fqty, logged = _sell_position_once(db, kraken, pos, "Position monitor hard stop")
                    if not logged:
                        log.append("    CRITICAL: SELL telemetry failed; state was closed to prevent duplicate SELL")
                    pnl_suffix = format_sell_pnl(pos["entry"], pos["current"], pos["qty"])
                    log.append(f"    ✅ Sold {fqty} {sym} @ ~€{pos['current']:.4f} {pnl_suffix}")
                    # Remove from trading_state
                except Exception as e:
                    log.append(f"    ❌ Sell failed: {e}")
            continue

        # Get price context + LLM decision
        price_ctx = _price_context(db, sym, ex)
        decision = _llm_decide(pos, price_ctx)
        log.append(f"    🤖 LLM: {decision['action']} (conf {decision['confidence']}/10) — {decision['reason']}")

        # Log to DB
        try:
            cur = db.cursor()
            cur.execute(
                """INSERT INTO llm_review_log
                   (strategy, symbol, price, score, verdict, reason, confidence,
                    portfolio_euro, available_euro)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (script_name, sym, pos["current"], 0,
                 decision["action"], decision["reason"], decision["confidence"],
                 sum(p["value"] for p in positions), 0),
            )
            db.commit()
        except Exception:
            pass

        # Execute SELL
        if decision["action"] == "SELL" and ex == "kraken":
            try:
                fqty, logged = _sell_position_once(
                    db, kraken, pos, f"Position monitor LLM SELL: {decision['reason']}")
                if not logged:
                    log.append("    CRITICAL: SELL telemetry failed; state was closed to prevent duplicate SELL")
                pnl_suffix = format_sell_pnl(pos["entry"], pos["current"], fqty)
                log.append(f"    ✅ Sold {fqty} {sym} {pnl_suffix}")
            except Exception as e:
                log.append(f"    ❌ Sell failed: {e}")

    # ── Done ─────────────────────────────────────────────────────
    db.close()
    for line in log:
        print(line)


if __name__ == "__main__":
    main()
