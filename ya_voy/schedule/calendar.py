"""``business_date`` (docs/03 §2, RF-09, D-16)."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo


def business_date(now: dt.datetime, tz: ZoneInfo, rollover: dt.time) -> dt.date:
    """Local date of ``now``, or the previous day before ``BUSINESS_DAY_ROLLOVER_TIME``."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(tz)
    if local.time() < rollover:
        return local.date() - dt.timedelta(days=1)
    return local.date()
