"""Builders for schedule tests (local wall-clock times in the configured TZ)."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from ya_voy.domain.enums import ScheduleOverrideKind, ScheduleOverrideStatus
from ya_voy.schedule.hours import OverrideView

BA = ZoneInfo("America/Argentina/Buenos_Aires")
D = dt.date(2026, 10, 9)  # the business day of most tests
K = ScheduleOverrideKind


def at(day: int, hour: int, minute: int = 0, *, month: int = 10, tz: ZoneInfo = BA) -> dt.datetime:
    return dt.datetime(2026, month, day, hour, minute, tzinfo=tz)


def override(
    kind: ScheduleOverrideKind,
    starts_at: dt.datetime | None,
    ends_at: dt.datetime | None,
    *,
    day: dt.date | None = D,
    reason: str | None = None,
    status: ScheduleOverrideStatus = ScheduleOverrideStatus.ACTIVE,
    cancelled_at: dt.datetime | None = None,
    id: int | None = None,
    confirmed_at: dt.datetime | None = None,
) -> OverrideView:
    """``confirmed_at`` decides precedence (most recent wins); ``None`` ranks as the newest."""
    if kind in (K.CLOSED_UNTIL_FURTHER_NOTICE, K.REOPEN):
        day = None
    return OverrideView(
        id, kind, status, day, starts_at, ends_at, reason, cancelled_at, confirmed_at
    )
