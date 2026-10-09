"""Business day arithmetic (docs/03 §2, RF-09, D-16).

The business day ``D`` runs from ``BUSINESS_DAY_ROLLOVER_TIME`` (05:00) local on ``D`` to the same
time on ``D + 1``. A wall-clock time of day *before* the rollover belongs to the next calendar day
(``00:00`` of ``D`` means 24:00, ``01:00`` means 25:00). Every instant is built from local wall
clock + ``ZoneInfo`` and returned in UTC, so days with a DST change simply have 23 or 25 hours
(arithmetic and comparisons between datetimes sharing one ``ZoneInfo`` use wall-clock time in
Python, which is wrong across a DST change; UTC avoids it).
"""

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


def local_at(day: dt.date, time: dt.time, tz: ZoneInfo, rollover: dt.time) -> dt.datetime:
    """The instant of ``time`` within business day ``day`` (before the rollover = next day)."""
    calendar_day = day + dt.timedelta(days=1) if time < rollover else day
    return dt.datetime.combine(calendar_day, time, tzinfo=tz).astimezone(dt.UTC)


def business_day_bounds(
    day: dt.date, tz: ZoneInfo, rollover: dt.time
) -> tuple[dt.datetime, dt.datetime]:
    """``[rollover of day, rollover of day + 1)``."""
    start = dt.datetime.combine(day, rollover, tzinfo=tz).astimezone(dt.UTC)
    end = dt.datetime.combine(day + dt.timedelta(days=1), rollover, tzinfo=tz).astimezone(dt.UTC)
    return start, end
