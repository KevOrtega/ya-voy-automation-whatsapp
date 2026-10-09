"""Effective schedule of a business day (RF-07, RF-08, RN-18, RN-25, RN-26; docs/03 §2).

Pure code: callers load the ``schedule_overrides`` rows (see ``repository``) and pass them in as
:class:`OverrideView`.

Precedence (RF-08, RN-25): **the most recently confirmed instruction wins** where a closure and an
opening overlap. The base hours are the bottom layer; the applied overrides are painted on top in
:meth:`OverrideView.rank` order (``confirmed_at``, then ``id``): an ``open_window`` makes its period
open, a closure makes its period closed. Among the closures that win at an instant, the widest one
is reported (``closed_until_further_notice`` > ``closed_today`` > ``closed_window``, CU-10 3a).
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
FAR_PAST = dt.datetime.min.replace(tzinfo=dt.UTC)
FAR_FUTURE = dt.datetime.max.replace(tzinfo=dt.UTC)
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
    confirmed_at: dt.datetime | None = None

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
            confirmed_at=row.confirmed_at,
        )

    @property
    def rank(self) -> tuple[bool, dt.datetime, bool, int]:
        """Precedence order: later ``confirmed_at`` wins, then the higher ``id``. Not yet confirmed
        (a proposal being evaluated) or not yet stored ranks above everything already applied."""
        return (
            self.confirmed_at is None,
            self.confirmed_at or FAR_PAST,
            self.id is None,
            self.id or 0,
        )

    @property
    def is_opening(self) -> bool:
        return self.kind == ScheduleOverrideKind.OPEN_WINDOW

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


def _widest_first(closures: Iterable[OverrideView]) -> list[OverrideView]:
    return sorted(closures, key=lambda o: (CLOSURE_PRIORITY[o.kind], o.starts_at))


def closures_in_effect(overrides: Iterable[OverrideView], at: dt.datetime) -> list[OverrideView]:
    """Every closure in effect at ``at``, even one a later opening overrides; widest first.

    ``reopen`` lifts these (RF-44)."""
    return _widest_first(o for o in overrides if o.is_closure and o.in_effect(at))


def winning_closures(overrides: Iterable[OverrideView], at: dt.datetime) -> list[OverrideView]:
    """Closures in effect at ``at`` confirmed after every opening in effect at ``at``: the ones
    that actually close the store then (most recent wins). Widest first (CU-10 3a)."""
    overrides = list(overrides)
    openings = [o.rank for o in overrides if o.is_opening and o.in_effect(at)]
    newest_opening = max(openings, default=None)
    return _widest_first(
        o
        for o in closures_in_effect(overrides, at)
        if newest_opening is None or o.rank > newest_opening
    )


def decide_acceptance(
    settings: Settings, overrides: Iterable[OverrideView], at: dt.datetime
) -> AcceptanceDecision:
    """``is_accepting_orders`` (docs/03 §2): (1) a closure in effect confirmed after every
    opening in effect → closed with it (the widest such); (2) inside the base hours of the business
    day or an opening in effect → open; (3) outside hours."""
    overrides = list(overrides)
    day = business_date(at, settings.zoneinfo, settings.business_day_rollover_time)
    closures = winning_closures(overrides, at)
    if closures:
        return AcceptanceDecision(False, AcceptanceReason.CLOSED, day, closures[0])
    opening = any(o.is_opening and o.in_effect(at) for o in overrides)
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
    """Open periods of business day ``day``: the base hours, then every applied override touching
    the day painted on top in :attr:`OverrideView.rank` order (most recent wins)."""
    bounds = day_bounds(settings, day)
    layers = sorted(
        (
            o
            for o in overrides
            if (o.is_closure or o.is_opening)
            and o.is_applied
            and o.interval(bounds.end).intersect(bounds)
        ),
        key=lambda o: o.rank,
    )
    state = intersect_all([base_interval(settings, day)], bounds)
    for o in layers:
        period = o.interval(bounds.end)
        state = union([*state, period]) if o.is_opening else subtract(state, [period])
    return intersect_all(state, bounds)


def overridden_in_overlap(
    overrides: Iterable[OverrideView], new: OverrideView, now: dt.datetime
) -> list[OverrideView]:
    """Applied overrides of the opposite polarity that ``new`` overrides from ``now`` on: those
    that are the top layer somewhere inside ``new``'s period (``{replaced_list}``)."""
    overrides = [o for o in overrides if o.is_applied and (o.is_closure or o.is_opening)]
    if new.starts_at is None:
        return []
    target = Interval(max(new.starts_at, now), new.ends_at or FAR_FUTURE)
    if target.is_empty:
        return []
    found: list[OverrideView] = []
    for o in sorted(overrides, key=lambda o: o.rank):
        if o.is_closure == new.is_closure or o.rank >= new.rank:
            continue
        above = [p.interval(FAR_FUTURE) for p in overrides if p.rank > o.rank]
        visible = subtract([o.interval(FAR_FUTURE)], above)
        if intersect_all(visible, target):
            found.append(o)
    return found


def report_due_at(
    settings: Settings, overrides: Iterable[OverrideView], day: dt.date
) -> dt.datetime:
    """Effective close of ``day``: the later of the base end and the end of any applied opening.
    Closures never move it earlier, even one confirmed after the opening (it can be lifted, and a
    late report with zero orders is harmless)."""
    ends = [base_interval(settings, day).end]
    ends += [o.ends_at for o in openings_of(overrides, day) if o.ends_at is not None]
    return max(ends)
