"""Key/value settings over app_settings table with 30s TTL cache.

    python -m app.settings  # self-check round-trip
"""

from __future__ import annotations

import os
import time

from app.db import get_conn

# ponytail: simple dict cache with 30s TTL — drop if staleness ever matters
_cache: dict[str, tuple[float, str]] = {}
_TTL = 30  # seconds


def get_setting(key: str, default: str | None = None) -> str | None:
    now = time.monotonic()
    if key in _cache:
        ts, val = _cache[key]
        if now - ts < _TTL:
            return val
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT value FROM app_settings WHERE key = %s", (key,))
        row = cur.fetchone()
        val = row[0] if row else default
        if val is not None:
            _cache[key] = (now, val)
        return val


def set_setting(key: str, value: str) -> None:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (%s, %s, now()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            (key, value),
        )

    _cache[key] = (time.monotonic(), value)


def get_ai_config() -> dict[str, str | None]:
    """Return {model, base_url, api_key} — DB first, then env, then defaults."""
    model = get_setting("ai.model") or os.getenv("AI_MODEL") or "hermes-flash"
    base_url = get_setting("ai.base_url") or os.getenv("LITELLM_BASE_URL") or "http://localhost:4000/v1"
    api_key = get_setting("ai.api_key") or os.getenv("DEEPSEEK_API_KEY")
    return {"model": model, "base_url": base_url, "api_key": api_key}


def seed_default_settings() -> None:
    """Ensure default settings exist in app_settings (idempotent)."""
    defaults = {
        # App log level: DEBUG|INFO|WARNING|ERROR — uvicorn access is always DEBUG severity
        "logging.level": "INFO",
        # LLM trade-review gating: shadow (consult + log, never block — default
        # since 2026-08-20, LLM accuracy worse than random) | gate | off.
        "llm.review_mode": "shadow",
    }
    for key, val in defaults.items():
        if get_setting(key) is None:
            set_setting(key, val)


if __name__ == "__main__":
    # Self-check: set + get round-trip
    assert os.environ.get("DB_HOST"), "DB_HOST not set — source .env first"
    set_setting("_test_roundtrip", "ok")
    assert get_setting("_test_roundtrip") == "ok", "round-trip failed"
    print("app.settings: OK")
