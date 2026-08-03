"""LLM prompt management — DB-backed with 15s TTL cache.

    python -m app.llm_prompts  # self-check round-trip
"""

from __future__ import annotations

import time
from app.db import get_conn

# ponytail: simple dict cache with 15s TTL — drop if staleness ever matters
_cache: dict[str, tuple[float, str]] = {}
_TTL = 15

DEFAULT_PROMPTS: dict[str, dict[str, str]] = {
    "trade_review_system": {
        "label": "Trade Review — System",
        "description": "System prompt for the trade review LLM call",
        "body": (
            "You are an AI trade reviewer for a {strategy} crypto/stock strategy. "
            "Reply with JSON only."
        ),
    },
    "trade_review_user": {
        "label": "Trade Review — User",
        "description": "User prompt with all trade context injected",
        "body": """You are an AI trade reviewer for a {strategy} crypto strategy.

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

{rules_block}""",
    },
    "trade_review_rules_normal": {
        "label": "Trade Review — Normal Rules",
        "description": "Default evaluation rules for non-high-risk strategies",
        "body": """Rules:
- Default to APPROVE when evidence is balanced; reject only for clear reasons.
- If score is negative or very low (< 2): lean REJECT.
- Reject only for clear reasons: extreme volatility, conflicting signals, tiny capital.
- Use price context: reject if the coin already pumped +15% in 6h (chasing top).
- Use the Market Strategy above to align with the macro view: if regime is bullish and pullback is aggressive, favor entries. If cautious/skip, be more selective.""",
    },
    "trade_review_rules_high_risk": {
        "label": "Trade Review — High-Risk Rules",
        "description": "Strict default-REJECT gate for high-risk strategies",
        "body": """Rules (HIGH-RISK STRATEGY — STRICT GATE):
- DEFAULT to REJECT. APPROVE only if the setup is unusually strong.
- APPROVE requires: clear multi-signal alignment, NOT chasing a parabolic spike,
  confidence-worthy momentum (6+ on at least 2 indicators), and a clean entry.
- REJECT ambiguous or balanced evidence — tie goes to REJECT.
- REJECT if the coin already pumped hard on short horizons (≥10% in 1h, ≥20% in 6h).
- REJECT if volume or momentum is fading even slightly.
- Bar is higher than normal momentum strategies. Only the clearest setups pass.""",
    },
    "position_monitor_system": {
        "label": "Position Monitor — System",
        "description": "System prompt for the position monitor LLM call",
        "body": "You are a position monitor. Reply with JSON only: action SELL or HOLD.",
    },
    "position_monitor_user": {
        "label": "Position Monitor — User",
        "description": "User prompt with position data and price context",
        "body": """You are a position monitor for a crypto/stock portfolio.
Decide whether to SELL or HOLD this position.

POSITION:
  Symbol: {symbol}
  Entry: €{entry:.4f}
  Current: €{current:.4f}
  P&L: {pnl_pct:+.2f}%
  Qty: {qty:.6f}
  Value: €{value:.2f}
  Exchange: {exchange}

PRICE CONTEXT:
{price_ctx}

Return JSON:
{{"action": "SELL"|"HOLD", "reason": "brief reason", "confidence": 1-10}}

Guidelines:
- SELL if the trade thesis is clearly broken (e.g., -5%+ loss with no recovery signs).
- HOLD if there's no clear reason to exit — small fluctuations are normal.
- If price is near entry and signals are neutral, default to HOLD.
- Be decisive — don't HOLD bleeding positions out of hope.""",
    },
}


def invalidate_prompt_cache(key: str | None = None) -> None:
    """Clear cached prompt(s). If key is None, clear all."""
    if key is None:
        _cache.clear()
    else:
        _cache.pop(key, None)


def get_default_body(key: str) -> str:
    """Return the hardcoded default body for a prompt key."""
    return DEFAULT_PROMPTS[key]["body"]


def get_prompt(key: str) -> str:
    """Return the prompt body for *key* (cache → DB → code default). Never returns empty."""
    now = time.monotonic()
    if key in _cache:
        ts, body = _cache[key]
        if now - ts < _TTL:
            return body

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT body FROM llm_prompts WHERE key = %s", (key,))
        row = cur.fetchone()

    body = row[0] if row and row[0] else get_default_body(key)
    _cache[key] = (now, body)
    return body


def list_prompts() -> list[dict]:
    """Return all prompt metadata — current body, default body, is_custom flag."""
    rows: list[dict] = []
    now = time.monotonic()

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT key, label, description, body, updated_at FROM llm_prompts")
        db_map = {row[0]: row for row in cur.fetchall()}

    for key, defn in DEFAULT_PROMPTS.items():
        db_row = db_map.get(key)
        body = db_row[3] if db_row else defn["body"]
        rows.append({
            "key": key,
            "label": db_row[1] if db_row else defn["label"],
            "description": db_row[2] if db_row else defn["description"],
            "body": body,
            "default_body": defn["body"],
            "is_custom": body != defn["body"],
            "updated_at": db_row[4].isoformat() if db_row and db_row[4] else None,
        })
    return rows


def save_prompt(key: str, body: str, *, label: str | None = None,
                description: str | None = None) -> None:
    """Upsert a prompt into the DB. Falls back to defaults for label/description if None."""
    if label is None or description is None:
        # Try existing DB row first, then defaults
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT label, description FROM llm_prompts WHERE key = %s", (key,))
            row = cur.fetchone()
        existing_label, existing_desc = row if row else (None, None)
        if label is None:
            label = existing_label or DEFAULT_PROMPTS.get(key, {}).get("label", key)
        if description is None:
            description = existing_desc or DEFAULT_PROMPTS.get(key, {}).get("description", "")

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO llm_prompts (key, label, description, body, updated_at)
               VALUES (%s, %s, %s, %s, now())
               ON CONFLICT (key) DO UPDATE SET
                 body = EXCLUDED.body,
                 label = EXCLUDED.label,
                 description = EXCLUDED.description,
                 updated_at = now()""",
            (key, label, description, body),
        )

    _cache.pop(key, None)


def reset_prompt(key: str) -> None:
    """Write the code default body back to the DB for *key*."""
    defn = DEFAULT_PROMPTS[key]
    save_prompt(key, defn["body"], label=defn["label"], description=defn["description"])


def seed_prompts() -> int:
    """Insert missing keys only (idempotent). Returns number of rows inserted."""
    count = 0
    for key, defn in DEFAULT_PROMPTS.items():
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM llm_prompts WHERE key = %s", (key,))
            if cur.fetchone():
                continue
            cur.execute(
                """INSERT INTO llm_prompts (key, label, description, body, updated_at)
                   VALUES (%s, %s, %s, %s, now())
                   ON CONFLICT (key) DO NOTHING""",
                (key, defn["label"], defn["description"], defn["body"]),
            )
            count += cur.rowcount
    return count


if __name__ == "__main__":
    import os
    assert os.environ.get("DB_HOST"), "DB_HOST not set — source .env first"

    # Round-trip check
    seed_prompts()
    body = get_prompt("trade_review_system")
    assert body, "get_prompt returned empty"
    assert "{strategy}" in body, "missing {strategy} placeholder"

    default = get_default_body("trade_review_system")
    save_prompt("trade_review_system", "Custom body for test")
    assert get_prompt("trade_review_system") == "Custom body for test"
    reset_prompt("trade_review_system")
    assert get_prompt("trade_review_system") == default

    prompts = list_prompts()
    assert len(prompts) == 6, f"expected 6 prompts, got {len(prompts)}"
    for p in prompts:
        assert p["body"], f"{p['key']} has empty body"

    invalidate_prompt_cache()
    print("app.llm_prompts: OK")
