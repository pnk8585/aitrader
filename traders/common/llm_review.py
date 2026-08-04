#!/usr/bin/env python3
"""Synchronous LLM trade review — instant APPROVE/REJECT without Overseer.

Called by trading scripts when they find a candidate that needs AI evaluation.
Returns verdict in ~2-3 seconds via local LiteLLM proxy.

Usage:
    from traders.common.llm_review import review_trade

    verdict = review_trade(
        symbol="NEAR/EUR",
        strategy="pullback",
        signals={"t3": 1.85, "rng": 5.4, "pullback": 0.9},
        price=1.2345,
        score=5.2,
    )
    # verdict = {"verdict": "APPROVE", "reason": "...", "confidence": 6}
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import URLError

from dotenv import load_dotenv
from openai import OpenAI

from app.llm_prompts import get_prompt

# Load env — trading scripts may load it already, but be safe
_ENV_LOADED = False


def _ensure_env():
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    # Try the trade project .env first, then Hermes global
    for p in [
        "/home/pank/projects/aitrader/.env",
        "/home/pank/.hermes/.env",
    ]:
        if os.path.exists(p):
            load_dotenv(p, override=False)
    _ENV_LOADED = True


# ── Config ──────────────────────────────────────────────────────────
# ponytail: DB-first with env fallback; keep hardcoded default as last resort

def _resolve_config():
    """Resolve AI model, base_url, api_key. DB → env → defaults. Never crashes."""
    _ensure_env()
    model = os.getenv("AI_MODEL") or "hermes-flash"
    base_url = os.getenv("LITELLM_BASE_URL") or "http://localhost:4000/v1"
    api_key = os.getenv("DEEPSEEK_API_KEY")

    try:
        from app.settings import get_ai_config
        cfg = get_ai_config()
        model = cfg.get("model") or model
        base_url = cfg.get("base_url") or base_url
        api_key = cfg.get("api_key") or api_key
    except Exception:
        pass

    return model, base_url, api_key


def _get_client():
    model, base_url, api_key = _resolve_config()
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY not found in environment or settings")
    return OpenAI(api_key=api_key, base_url=base_url), model


def _rules_key_for_strategy(strategy: str) -> str:
    """Pick the DB/code prompt key for this strategy family."""
    s = (strategy or "").lower()
    if "high-risk" in s:
        return "trade_review_rules_high_risk"
    if "stock" in s:
        return "trade_review_rules_stocks_momentum"
    return "trade_review_rules_normal"


def _price_context_from_signals(signals: dict) -> str:
    """Fallback when asset_prices has no history (typical for US stocks)."""
    if not signals:
        return ""
    lines = ["From scanner signals (no DB multi-horizon history):"]
    mapping = (
        ("daily_pct", "daily change"),
        ("intraday_pct", "intraday change"),
        ("strength", "strength"),
        ("mult", "sizing mult"),
    )
    for key, label in mapping:
        if key in signals and signals[key] is not None:
            val = signals[key]
            if isinstance(val, (int, float)) and key.endswith("pct"):
                lines.append(f"  {label}: {float(val):+.2f}%")
            else:
                lines.append(f"  {label}: {val}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _extract_json_object(text: str) -> str | None:
    """Pull a JSON object out of model text (bettips-ai predictor style).

    Handles empty content, markdown fences, preamble, and trailing junk.
    Returns the raw object substring or None if nothing looks like JSON.
    """
    if not text or not str(text).strip():
        return None
    content = str(text).strip()
    # Fenced ```json ... ``` (non-greedy body)
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # Any object span (greedy — last closing brace wins for trailing notes)
    m = re.search(r"(\{.*\})", content, re.DOTALL)
    if m:
        return m.group(1).strip()
    return None


def _message_text(msg) -> str:
    """content first, then reasoning_content (GLM/DeepSeek via LiteLLM)."""
    raw = (getattr(msg, "content", None) or "").strip()
    if raw:
        return raw
    return (getattr(msg, "reasoning_content", None) or "").strip()


def review_trade(
    symbol: str,
    strategy: str,
    signals: dict,
    price: float,
    score: float,
    portfolio_euro: float = 0,
    available_euro: float = 0,
    open_positions: int = 0,
    timeout: int = 15,
    db_conn=None,  # optional — for price history enrichment
) -> dict:
    """Call LLM to evaluate a trade candidate. Returns {verdict, reason, confidence}."""
    client, model = _get_client()

    # ── Price context from DB, else signals ──────────────────────────
    price_ctx = ""
    if db_conn:
        try:
            price_ctx = _build_price_context(db_conn, symbol, price)
        except Exception as e:
            price_ctx = f"(price context unavailable: {e})"
    if not price_ctx or price_ctx.startswith("(no price") or price_ctx.startswith("(price context"):
        fallback = _price_context_from_signals(signals)
        if fallback:
            price_ctx = fallback if not price_ctx else f"{price_ctx}\n{fallback}"

    # ── News context (parallel, 4s timeout) ───────────────────────────
    news_ctx = ""
    try:
        news_ctx = _fetch_news_parallel(symbol, timeout=4)
    except Exception as e:
        news_ctx = f"(news unavailable: {e})"

    # ── Market strategy context (from daily Market Architect) ──────────
    strategy_ctx = ""
    try:
        from traders.common.market_architect import get_strategy
        strat = get_strategy()
        if strat:
            strategy_ctx = (
                f"MARKET STRATEGY (Market Architect):\n"
                f"  Market regime: {strat.get('market_regime', 'unknown')}\n"
                f"  Risk level: {strat.get('risk_level', 'unknown')}\n"
                f"  BTC regime: {strat.get('btc_regime', 'unknown')}\n"
                f"  Pullback: {strat.get('pullback_adjustment', 'normal')}\n"
                f"  Momentum: {strat.get('momentum_adjustment', 'normal')}\n"
                f"  Max daily buys: {strat.get('max_daily_buys', 3)}\n"
                f"  Geopolitical risk: {strat.get('geopolitical_risk', 'unknown')}\n"
                f"  Macro events: {', '.join(strat.get('macro_events_today', [])) or 'none'}\n"
                f"  Strategy notes: {strat.get('strategy_notes', '')[:200]}\n"
            )
        else:
            strategy_ctx = "(Market Architect: strategy stale, regenerating — trade without macro context)\n"
    except Exception as e:
        strategy_ctx = f"(Market Architect unavailable: {e})\n"

    sig_str = "\n".join(f"  {k}: {v}" for k, v in sorted(signals.items()))

    # ── Rules by strategy family ───────────────────────────────────────
    rules_key = _rules_key_for_strategy(strategy)
    rules_block = get_prompt(rules_key)

    system_prompt = get_prompt("trade_review_system").format(strategy=strategy)
    prompt = get_prompt("trade_review_user").format(
        strategy=strategy,
        symbol=symbol,
        price=price,
        score=score,
        portfolio_euro=portfolio_euro,
        available_euro=available_euro,
        open_positions=open_positions,
        sig_str=sig_str,
        price_ctx=price_ctx,
        news_ctx=news_ctx,
        strategy_ctx=strategy_ctx,
        rules_block=rules_block,
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
            # 300 was too tight when models leak a short preamble before JSON
            max_tokens=600,
            timeout=timeout,
        )
        latency_ms = (time.monotonic() - t0) * 1000
        # DeepSeek/GLM via LiteLLM: never use response_format=json_object (empty
        # content). Mirror bettips-ai predictor.py — content → reasoning_content,
        # then regex-extract the JSON object.
        msg = resp.choices[0].message
        raw_full = _message_text(msg)
        extracted = _extract_json_object(raw_full)
        if extracted is None:
            print(f"LLM returned text (no JSON): {raw_full[:200]!r}", file=sys.stderr)
            result = {
                "verdict": "REJECT",
                "reason": "LLM returned text, defaulting",
                "confidence": 5,
            }
        else:
            try:
                result = json.loads(extracted)
            except json.JSONDecodeError:
                print(f"LLM returned non-JSON: {extracted[:200]}", file=sys.stderr)
                result = {
                    "verdict": "REJECT",
                    "reason": f"LLM parse error: {extracted[:80]}",
                    "confidence": 5,
                }
        # Normalize verdict casing from sloppy models
        verdict = str(result.get("verdict", "REJECT")).strip().upper()
        if verdict not in ("APPROVE", "REJECT"):
            verdict = "REJECT"
        try:
            conf = int(result.get("confidence", 5))
        except (TypeError, ValueError):
            conf = 5
        final = {
            "verdict": verdict,
            "reason": str(result.get("reason", "No reason given"))[:200],
            "confidence": max(1, min(10, conf)),
        }
        _log_llm_jsonl(
            kind="trade_review",
            model=model,
            system_prompt=system_prompt,
            user_prompt=prompt,
            response=raw_full,
            status="ok",
            latency_ms=latency_ms,
            symbol=symbol,
            strategy=strategy,
            final=final,
            extra={
                "price": price,
                "score": score,
                "signals": signals,
                "portfolio_euro": portfolio_euro,
                "available_euro": available_euro,
                "open_positions": open_positions,
            },
        )
        _log_review(symbol, strategy, price, score, signals,
                    portfolio_euro, available_euro, final)
        _notify_verdict(final, symbol, strategy, price)
        return final
    except Exception as e:
        latency_ms = (time.monotonic() - t0) * 1000
        final = {"verdict": "REJECT", "reason": f"LLM error: {str(e)[:80]}", "confidence": 5}
        _log_llm_jsonl(
            kind="trade_review",
            model=model,
            system_prompt=system_prompt,
            user_prompt=prompt,
            response=None,
            status="error",
            error=str(e),
            latency_ms=latency_ms,
            symbol=symbol,
            strategy=strategy,
            final=final,
            extra={"price": price, "score": score},
        )
        _log_review(symbol, strategy, price, score, signals,
                    portfolio_euro, available_euro, final)
        _notify_verdict(final, symbol, strategy, price)
        return final


def _notify_verdict(verdict: dict, symbol: str, strategy: str, price: float) -> None:
    """Best-effort Telegram notification. Lazy import — never raises into the trade path."""
    try:
        from app.notify import send_telegram
    except Exception:
        return
    try:
        v = verdict.get("verdict", "?")
        conf = verdict.get("confidence", "?")
        emoji = "✅" if v == "APPROVE" else "❌" if v == "REJECT" else "⚠️"
        msg = f"{emoji} {v}: {symbol} ({strategy}) @ €{price:.4f} conf={conf}"
        send_telegram(msg)
    except Exception:
        pass


def _log_llm_jsonl(
    *,
    kind: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    response: str | None,
    status: str,
    latency_ms: float,
    symbol: str,
    strategy: str,
    final: dict,
    error: str | None = None,
    extra: dict | None = None,
) -> None:
    """Structured audit line → logs/llm.jsonl (bettips-ai style). Never raises."""
    try:
        from app.logging_setup import log_llm_call
        payload = dict(extra or {})
        payload.update({
            "verdict": final.get("verdict"),
            "reason": final.get("reason"),
            "confidence": final.get("confidence"),
        })
        log_llm_call(
            kind=kind,
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response=response,
            status=status,
            error=error,
            latency_ms=latency_ms,
            symbol=symbol,
            strategy=strategy,
            extra=payload,
        )
    except Exception:
        pass


def _log_review(symbol, strategy, price, score, signals,
                portfolio_euro, available_euro, result):
    """Persist review to DB for later analysis (was this rejection correct?)."""
    try:
        import psycopg2
        from dotenv import load_dotenv
        load_dotenv("/home/pank/projects/aitrader/.env", override=False)
        load_dotenv("/home/pank/.hermes/.env", override=False)

        conn = psycopg2.connect(
            host=os.environ["DB_HOST"],
            port=int(os.environ.get("DB_PORT", 5432)),
            dbname=os.environ["DB_NAME"],
            user=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"],
            connect_timeout=3,
        )
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO llm_review_log
               (strategy, symbol, price, score, verdict, reason, confidence,
                signals, portfolio_euro, available_euro, daily_strategy)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                strategy, symbol, price, score,
                result["verdict"], result["reason"], result["confidence"],
                json.dumps(signals) if signals else None,
                portfolio_euro, available_euro,
                None,  # daily_strategy removed upstream — column persisted as NULL
            ),
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"llm_review log failed: {e}", file=sys.stderr)


def _build_price_context(db_conn, symbol: str, current_price: float) -> str:
    """Query asset_prices for recent performance data. Returns formatted string."""
    cur = db_conn.cursor()
    lines = []

    # ── Coin price history (1h, 6h, 24h) ──────────────────────────
    base = symbol.split("/")[0]
    for label, minutes in [("1h", 60), ("6h", 360), ("24h", 1440)]:
        cur.execute(
            """SELECT price FROM asset_prices
               WHERE exchange='kraken' AND symbol=%s
               AND timestamp >= NOW() - (%s * INTERVAL '1 minute')
               ORDER BY timestamp LIMIT 1""",
            (base, minutes))
        row = cur.fetchone()
        if row:
            old_price = float(row[0])
            change = (current_price - old_price) / old_price * 100
            lines.append(f"  {label} ago: €{old_price:.4f} → now €{current_price:.4f} ({change:+.2f}%)")
        else:
            lines.append(f"  {label} ago: no data")

    # ── BTC price and trend ───────────────────────────────────────
    if base != "BTC":
        cur.execute(
            """SELECT price FROM asset_prices
               WHERE exchange='kraken' AND symbol='BTC'
               ORDER BY timestamp DESC LIMIT 72""")
        btc_rows = cur.fetchall()
        if btc_rows:
            btc_now = float(btc_rows[0][0])
            btc_avg = sum(float(r[0]) for r in btc_rows) / len(btc_rows)
            btc_change = (btc_now - btc_avg) / btc_avg * 100
            lines.append(f"  BTC: €{btc_now:,.0f} ({btc_change:+.1f}% vs 6h avg)")

    # ── 24h range ─────────────────────────────────────────────────
    cur.execute(
        """SELECT MIN(price), MAX(price) FROM asset_prices
           WHERE exchange='kraken' AND symbol=%s
           AND timestamp >= NOW() - INTERVAL '24 hours'""",
        (base,))
    row = cur.fetchone()
    if row and row[0] and row[1]:
        low, high = float(row[0]), float(row[1])
        pct_from_low = (current_price - low) / low * 100
        pct_from_high = (high - current_price) / high * 100
        lines.append(f"  24h range: €{low:.4f} – €{high:.4f} (now {pct_from_low:.1f}% above low, {pct_from_high:.1f}% below high)")

    return "\n".join(lines) if lines else "(no price data available)"


def _fetch_news_parallel(symbol: str, timeout: int = 4) -> str:
    """Fetch news headlines for a coin/stock with a hard timeout. Returns formatted string."""
    base = symbol.split("/")[0]
    query = f"{base} crypto news today" if "/" in symbol else f"{base} stock news today"
    result = [""]

    def _fetch():
        try:
            result[0] = _search_news(query)
        except Exception as e:
            result[0] = f"(search error: {e})"

    t = threading.Thread(target=_fetch, daemon=True)
    t.start()
    t.join(timeout=timeout)
    return result[0] if result[0] else "(no recent news found)"


def _search_news(query: str) -> str:
    """Search DuckDuckGo for news headlines. Returns up to 3 headlines, one per line."""
    url = f"https://html.duckduckgo.com/html/?q={query.replace(' ', '+')}"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(req, timeout=5) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except URLError:
        return "(search unavailable)"

    # Extract result snippets — DuckDuckGo HTML uses class='result__snippet'
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, re.DOTALL)
    if not snippets:
        # Try alternate pattern
        snippets = re.findall(r'class="result__snippet">(.*?)</', html, re.DOTALL)

    headlines = []
    for s in snippets[:5]:
        # Strip HTML tags
        clean = re.sub(r'<[^>]+>', '', s).strip()
        if clean and len(clean) > 15:
            headlines.append(f"  • {clean[:200]}")

    if not headlines:
        return "(no relevant news found)"
    return "\n".join(headlines[:3])


# ── CLI entry point (for standalone testing) ─────────────────────────
if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", required=True)
    p.add_argument("--strategy", default="pullback")
    p.add_argument("--score", type=float, default=5.0)
    p.add_argument("--price", type=float, default=1.0)
    p.add_argument("--signals", type=json.loads, default="{}")
    p.add_argument("--portfolio", type=float, default=0)
    p.add_argument("--available", type=float, default=0)
    p.add_argument("--positions", type=int, default=0)
    args = p.parse_args()

    result = review_trade(
        symbol=args.symbol,
        strategy=args.strategy,
        signals=args.signals,
        price=args.price,
        score=args.score,
        portfolio_euro=args.portfolio,
        available_euro=args.available,
        open_positions=args.positions,
    )
    print(json.dumps(result, indent=2))
