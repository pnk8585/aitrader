"""External market schedule checks used by the DB cron scheduler."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import requests

from traders.common.config import ALPACA_BASE_URL


def alpaca_market_status() -> tuple[bool, datetime | None, str]:
    """Return ``(is_open, next_open, reason)`` from Alpaca's market clock.

    A failed or incomplete clock check is fail-closed: the scheduler must not
    start a stock scan when it cannot prove that the market is open.
    """
    key = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        return False, None, "missing Alpaca credentials"

    try:
        response = requests.get(
            f"{ALPACA_BASE_URL}/v2/clock",
            headers={
                "APCA-API-KEY-ID": key,
                "APCA-API-SECRET-KEY": secret,
            },
            timeout=5,
        )
        response.raise_for_status()
        payload = response.json()
        is_open = payload.get("is_open")
        if not isinstance(is_open, bool):
            return False, None, "Alpaca clock response missing is_open"

        next_open = None
        raw_next_open = payload.get("next_open")
        if raw_next_open:
            next_open = datetime.fromisoformat(raw_next_open.replace("Z", "+00:00"))
            if next_open.tzinfo is None:
                next_open = next_open.replace(tzinfo=timezone.utc)
            next_open = next_open.astimezone(timezone.utc)

        return is_open, next_open, "market open" if is_open else "market closed"
    except (requests.RequestException, ValueError, TypeError) as exc:
        return False, None, f"Alpaca clock unavailable: {type(exc).__name__}"


def deferred_next_run(next_open: datetime | None, interval: int) -> datetime:
    """Choose the next scheduler wake-up while the market is closed."""
    now = datetime.now(timezone.utc)
    if next_open and next_open > now:
        return next_open
    return now + timedelta(seconds=interval)
