"""Effective schedule of a business day (RF-07, RF-08, RN-18, RN-25, RN-26; docs/03 §2).

Pure code: callers load the ``schedule_overrides`` rows (see ``repository``) and pass them in as
:class:`OverrideView`. Precedence (RF-08, RN-25): effective hours = (base hours + active
``open_window``) minus active closures; **a closure always wins over an opening** where they
overlap.
Between closures in effect at the same instant, the widest one is reported
(``closed_until_further_notice`` > ``closed_today`` > ``closed_window``, CU-10 3a).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ya_voy.config import Settings
from ya_voy.domain.enums import ScheduleOverrideKind, ScheduleOverrideStatus
from ya_voy.schedule.calendar import business_date, business_day_bounds, local_at

CLOSURE_KINDS = frozenset(
    {
        ScheduleOverrideKind.CLOSED_TODAY,
        ScheduleOverrideKind.CLOSED_UNTIL_FURTHER_NOTICE,
        ScheduleOverrideKind.CLOSED_WINDOW,
    }
)
CLOSURE_PRIORITY = {
    ScheduleOverrideKind.CLOSED_UNTIL_FURTHER_NOTICE: 1,
    ScheduleOverrideKind.CLOSED_TODAY: 2,
    ScheduleOverrideKind.CLOSED_WINDOW: 3,
}


@dataclass(frozen=True, order=True)
class Interval:
    start: dt.datetime
    end: dt.datetime

    @property
    def is_empty(self) -> bool:
        return self.end <= self.start

    def contains(self, at: dt.datetime) -> bool:
        return self.start <= at < self.end

    def intersect(self, other: Interval) -> Interval | None:
        result = Interval(max(self.start, other.start), min(self.end, other.end))
        return None if result.is_empty else result


def union(intervals: Iterable[Interval]) -> list[Interval]:
    merged: list[Interval] = []
    for current in sorted(i for i in intervals if not i.is_empty):
        if merged and current.start <= merged[-1].end:
            last = merged[-1]
            merged[-1] = Interval(last.start, max(last.end, current.end))
        else:
            merged.append(current)
    return merged


def subtract(intervals: Sequence[Interval], cuts: Iterable[Interval]) -> list[Interval]:
    result = list(intervals)
    for cut in union(cuts):
        pieces: list[Interval] = []
        for interval in result:
            if cut.end <= interval.start or interval.end <= cut.start:
                pieces.append(interval)
                continue
            if interval.start < cut.start:
                pieces.append(Interval(interval.start, cut.start))
            if cut.end < interval.end:
                pieces.append(Interval(cut.end, interval.end))
        result = pieces
    return result


def intersect_all(intervals: Sequence[Interval], window: Interval) -> list[Interval]:
    return [part for i in intervals if (part := i.intersect(window)) is not None]


@dataclass(frozen=True)
class OverrideView:
    """The columns of a ``schedule_overrides`` row that the schedule needs."""

    id: int | None
    kind: ScheduleOverrideKind
    status: ScheduleOverrideStatus
    business_date: dt.date | None
    starts_at: dt.datetime | None
    ends_at: dt.datetime | None
    reason: str | None = None
    cancelled_at: dt.datetime | None = None

    @classmethod
    def of(cls, row: Any) -> OverrideView:
        return cls(
            id=row.id,
            kind=ScheduleOverrideKind(row.kind),
            status=ScheduleOverrideStatus(row.status),
            business_date=row.business_date,
            starts_at=row.starts_at,
            ends_at=row.ends_at,
            reason=row.reason,
            cancelled_at=row.cancelled_at,
        )

    @property
    def is_closure(self) -> bool:
        return self.kind in CLOSURE_KINDS

    @property
    def is_applied(self) -> bool:
        """Confirmed and not lifted (``active``; ended ones stay ``active`` too, docs/03)."""
        return (
            self.status == ScheduleOverrideStatus.ACTIVE
            and self.cancelled_at is None
            and self.starts_at is not None
        )

    def interval(self, open_end: dt.datetime) -> Interval:
        assert self.starts_at is not None
        return Interval(self.starts_at, self.ends_at or open_end)

    def in_effect(self, at: dt.datetime) -> bool:
        return (
            self.is_applied
            and self.starts_at is not None
            and self.starts_at <= at
            and (self.ends_at is None or at < self.ends_at)
        )


class AcceptanceReason(StrEnum):
    OPEN = "open"
    OUTSIDE_HOURS = "outside_hours"  # store_out_of_hours
    CLOSED = "closed"  # an admin closure: store_closed_* by ``closure.kind``


@dataclass(frozen=True)
class AcceptanceDecision:
    accepting: bool
    reason: AcceptanceReason
    business_date: dt.date
    closure: OverrideView | None = None


def day_bounds(settings: Settings, day: dt.date) -> Interval:
    start, end = business_day_bounds(day, settings.zoneinfo, settings.business_day_rollover_time)
    return Interval(start, end)


def at_local(settings: Settings, day: dt.date, time: dt.time) -> dt.datetime:
    return local_at(day, time, settings.zoneinfo, settings.business_day_rollover_time)


def base_interval(settings: Settings, day: dt.date) -> Interval:
    """``BUSINESS_HOURS_START``-``BUSINESS_HOURS_END`` of ``day`` (an end before the rollover is
    after midnight)."""
    return Interval(
        at_local(settings, day, settings.business_hours_start),
        at_local(settings, day, settings.business_hours_end),
    )


def closures_in_effect(overrides: Iterable[OverrideView], at: dt.datetime) -> list[OverrideView]:
    """Closures in effect at ``at``, widest first (CU-10 3a)."""
    found = [o for o in overrides if o.is_closure and o.in_effect(at)]
    return sorted(found, key=lambda o: (CLOSURE_PRIORITY[o.kind], o.starts_at))


def decide_acceptance(
    settings: Settings, overrides: Iterable[OverrideView], at: dt.datetime
) -> AcceptanceDecision:
    """``is_accepting_orders`` (docs/03 §2): (1) closure in effect → closed with it; (2) inside
    the base hours of the business day or an opening in effect → open; (3) outside hours."""
    overrides = list(overrides)
    day = business_date(at, settings.zoneinfo, settings.business_day_rollover_time)
    closures = closures_in_effect(overrides, at)
    if closures:
        return AcceptanceDecision(False, AcceptanceReason.CLOSED, day, closures[0])
    opening = any(o.kind == ScheduleOverrideKind.OPEN_WINDOW and o.in_effect(at) for o in overrides)
    if opening or base_interval(settings, day).contains(at):
        return AcceptanceDecision(True, AcceptanceReason.OPEN, day)
    return AcceptanceDecision(False, AcceptanceReason.OUTSIDE_HOURS, day)


def openings_of(overrides: Iterable[OverrideView], day: dt.date) -> list[OverrideView]:
    return sorted(
        (
            o
            for o in overrides
            if o.kind == ScheduleOverrideKind.OPEN_WINDOW
            and o.is_applied
            and o.business_date == day
        ),
        key=lambda o: o.starts_at or dt.datetime.min.replace(tzinfo=dt.UTC),
    )


def closures_touching(
    settings: Settings, overrides: Iterable[OverrideView], day: dt.date
) -> list[OverrideView]:
    """Applied closures that overlap business day ``day``, in start order."""
    bounds = day_bounds(settings, day)
    return sorted(
        (
            o
            for o in overrides
            if o.is_closure and o.is_applied and o.interval(bounds.end).intersect(bounds)
        ),
        key=lambda o: o.starts_at or bounds.start,
    )


def effective_intervals(
    settings: Settings, overrides: Iterable[OverrideView], day: dt.date
) -> list[Interval]:
    """(Base + openings of ``day``) minus closures, within the business day (closures win)."""
    overrides = list(overrides)
    bounds = day_bounds(settings, day)
    opens = [base_interval(settings, day)]
    opens += [o.interval(bounds.end) for o in openings_of(overrides, day)]
    cuts = [o.interval(bounds.end) for o in closures_touching(settings, overrides, day)]
    return intersect_all(subtract(union(opens), cuts), bounds)


def report_due_at(
    settings: Settings, overrides: Iterable[OverrideView], day: dt.date
) -> dt.datetime:
    """Effective close of ``day``: the later of the base end and the end of any applied opening.
    Closures never move it earlier (they can be lifted)."""
    ends = [base_interval(settings, day).end]
    ends += [o.ends_at for o in openings_of(overrides, day) if o.ends_at is not None]
    return max(ends)
