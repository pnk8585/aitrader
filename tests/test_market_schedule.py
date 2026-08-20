from datetime import datetime, timedelta, timezone

from app.market_schedule import deferred_next_run


def test_deferred_schedule_uses_alpaca_next_open():
    # Use a FUTURE next_open (the function only honors next_open when it is
    # later than now). A hardcoded past date made this a time-bomb.
    next_open = datetime.now(timezone.utc) + timedelta(hours=1)
    assert deferred_next_run(next_open, 300) == next_open


def test_deferred_schedule_falls_back_to_interval_when_next_open_missing():
    result = deferred_next_run(None, 300)
    now = datetime.now(timezone.utc)
    assert 295 <= (result - now).total_seconds() <= 305