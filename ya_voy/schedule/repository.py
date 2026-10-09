"""Database reads for the schedule; the decisions are pure code in ``hours``.

``is_accepting_orders`` is the RN-18 gate stage 4b calls before touching drafts (CU-10).
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ya_voy.config import Settings
from ya_voy.db.models import DailyReport, ScheduleOverride
from ya_voy.domain.enums import ScheduleOverrideStatus
from ya_voy.schedule.calendar import business_date
from ya_voy.schedule.hours import (
    AcceptanceDecision,
    Interval,
    OverrideView,
    day_bounds,
    decide_acceptance,
    effective_intervals,
)


async def db_now(session: AsyncSession) -> dt.datetime:
    now = await session.scalar(select(func.now()))
    assert now is not None
    return now


async def load_overrides(
    session: AsyncSession, settings: Settings, now: dt.datetime
) -> list[OverrideView]:
    """``active`` overrides that can still matter from yesterday's business day on."""
    today = business_date(now, settings.zoneinfo, settings.business_day_rollover_time)
    since = day_bounds(settings, today - dt.timedelta(days=1)).start
    rows = await session.scalars(
        select(ScheduleOverride)
        .where(
            ScheduleOverride.status == ScheduleOverrideStatus.ACTIVE,
            or_(ScheduleOverride.ends_at.is_(None), ScheduleOverride.ends_at > since),
        )
        .order_by(ScheduleOverride.id)
    )
    return [OverrideView.of(row) for row in rows]


async def is_accepting_orders(
    session: AsyncSession, settings: Settings, at: dt.datetime | None = None
) -> AcceptanceDecision:
    """RN-18 / CU-10 step 1 at ``at`` (default: the database's ``now()``)."""
    now = at or await db_now(session)
    return decide_acceptance(settings, await load_overrides(session, settings, now), now)


async def todays_hours(
    session: AsyncSession, settings: Settings, at: dt.datetime | None = None
) -> list[Interval]:
    """Effective hours of the current business day (``{effective_hours}`` of store replies)."""
    now = at or await db_now(session)
    today = business_date(now, settings.zoneinfo, settings.business_day_rollover_time)
    return effective_intervals(settings, await load_overrides(session, settings, now), today)


async def report_status(session: AsyncSession, day: dt.date) -> str | None:
    return await session.scalar(select(DailyReport.status).where(DailyReport.report_date == day))
