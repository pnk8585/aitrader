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

    # ── Price context from DB ────────────────────────────────────────
    price_ctx = ""
    if db_conn:
        try:
            price_ctx = _build_price_context(db_conn, symbol, price)
        except Exception as e:
            price_ctx = f"(price context unavailable: {e})"

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

    # ── Rules: high-risk strategies get a stricter default-REJECT gate ───
    if "high-risk" in strategy.lower():
        rules_block = (
            "Rules (HIGH-RISK STRATEGY — STRICT GATE):\n"
            "- DEFAULT to REJECT. APPROVE only if the setup is unusually strong.\n"
            "- APPROVE requires: clear multi-signal alignment, NOT chasing a parabolic spike,\n"
            "  confidence-worthy momentum (6+ on at least 2 indicators), and a clean entry.\n"
            "- REJECT ambiguous or balanced evidence — tie goes to REJECT.\n"
            "- REJECT if the coin already pumped hard on short horizons (≥10% in 1h, ≥20% in 6h).\n"
            "- REJECT if volume or momentum is fading even slightly.\n"
            "- Bar is higher than normal momentum strategies. Only the clearest setups pass."
        )
    else:
        rules_block = (
            "Rules:\n"
            "- Default to APPROVE when evidence is balanced; reject only for clear reasons.\n"
            "- If score is negative or very low (< 2): lean REJECT.\n"
            "- Reject only for clear reasons: extreme volatility, conflicting signals, tiny capital.\n"
            "- Use price context: reject if the coin already pumped +15% in 6h (chasing top).\n"
            "- Use the Market Strategy above to align with the macro view: if regime is bullish and pullback is aggressive, favor entries. If cautious/skip, be more selective."
        )

    system_prompt = (
        f"You are an AI trade reviewer for a {strategy} crypto/stock strategy. "
        "Reply with JSON only."
    )
    prompt = f"""You are an AI trade reviewer for a {strategy} crypto strategy.

CONTEXT:
- Symbol: {symbol}
- Current price: €{price:.4f}
- Strategy: {strategy}
- Score: {score:.2f}
- Portfolio: €{portfolio_euro:.2f}
- Available EUR: €{available_euro:.2f}
- Open positions: {open_positions}

SIGNALS:
{sig_str}

PRICE CONTEXT:
{price_ctx}

NEWS:
{news_ctx}

{strategy_ctx}
Evaluate this candidate. Return JSON:
{{"verdict": "APPROVE"|"REJECT", "reason": "brief reason", "confidence": 1-10}}

{rules_block}"""

    t0 = time.monotonic()
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=300,
            timeout=timeout,
        )
        latency_ms = (time.monotonic() - t0) * 1000
        # DeepSeek/GLM upstreams via LiteLLM return empty `content` when
        # response_format=json_object is set (and some put the answer in
        # reasoning_content). Mirror bettips-ai predictor.py: no
        # response_format, fall back to reasoning_content, robust brace search.
        msg = resp.choices[0].message
        raw_full = (msg.content or "").strip()
        if not raw_full:
            raw_full = (getattr(msg, "reasoning_content", "") or "").strip()
        raw = raw_full
        # Strip markdown fences and leading/trailing noise
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
            if raw.endswith("```"):
                raw = raw[:-3]
        raw = raw.strip()
        # Some models prepend newlines — find the first '{'
        brace = raw.find("{")
        if brace > 0:
            raw = raw[brace:]
        elif brace < 0:
            # No JSON found — empty or text-only response
            raw = '{"verdict": "REJECT", "reason": "LLM returned text, defaulting", "confidence": 5}'
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            print(f"LLM returned non-JSON: {raw[:200]}", file=sys.stderr)
            result = {"verdict": "REJECT", "reason": f"LLM parse error: {raw[:80]}", "confidence": 5}
        final = {
            "verdict": result.get("verdict", "REJECT"),
            "reason": result.get("reason", "No reason given"),
            "confidence": int(result.get("confidence", 5)),
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
