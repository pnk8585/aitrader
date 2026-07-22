"""Telegram notification via Bot API.

    python -m app.notify  # self-check: no-op when disabled
"""

from __future__ import annotations

import logging

import httpx

from app.settings import get_setting

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def _config() -> tuple[str, str, bool]:
    token = get_setting("telegram.token") or ""
    chat_id = get_setting("telegram.chat_id") or ""
    enabled = get_setting("telegram.enabled") == "true"
    return token, chat_id, enabled


def send_telegram(text: str) -> bool:
    """Send a Telegram message. No-op when disabled. Swallows all errors."""
    token, chat_id, enabled = _config()
    if not enabled or not token or not chat_id:
        if not enabled:
            logger.debug("Telegram disabled, skipping send")
        else:
            logger.warning("Telegram enabled but token/chat_id missing")
        return False
    try:
        url = TELEGRAM_API.format(token=token)
        r = httpx.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                       timeout=4)
        r.raise_for_status()
        return True
    except Exception:
        logger.exception("Telegram send failed")
        return False


def send_test() -> dict:
    """Send a test message. Returns {ok: bool, error: str|None} for the UI."""
    token, chat_id, enabled = _config()
    if not enabled:
        return {"ok": False, "error": "Telegram is disabled"}
    if not token or not chat_id:
        return {"ok": False, "error": "Token and chat ID are required"}
    try:
        url = TELEGRAM_API.format(token=token)
        r = httpx.post(url, json={"chat_id": chat_id, "text": "AITrader test message", "parse_mode": "HTML"},
                       timeout=4)
        r.raise_for_status()
        return {"ok": True, "error": None}
    except httpx.HTTPStatusError as e:
        detail = e.response.text[:200] if e.response else str(e)
        return {"ok": False, "error": f"Telegram API error: {detail}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    token, chat_id, enabled = _config()
    print(f"Telegram: enabled={enabled}, token={'***' if token else 'unset'}, chat_id={chat_id or 'unset'}")
    if not enabled:
        assert send_telegram("test") is False, "should no-op when disabled"
        print("No-op path: OK")
    else:
        print("Enabled — run send_test() manually to verify")
