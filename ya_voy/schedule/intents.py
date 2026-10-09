"""``validate_schedule_intent`` (docs/05 "Esquema de interpretación de horario", CU-14 step 2).

Deterministic: the LLM only proposes a :class:`ScheduleOverrideIntent`; this module decides whether
it is valid and computes the concrete period, using the overrides currently applied.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum

from ya_voy.config import Settings
from ya_voy.domain.enums import ScheduleOverrideKind, ScheduleOverrideStatus
from ya_voy.llm.schemas import ScheduleOverrideIntent
from ya_voy.schedule.calendar import business_date
from ya_voy.schedule.hours import (
    Interval,
    OverrideView,
    at_local,
    closures_in_effect,
    closures_touching,
    day_bounds,
    effective_intervals,
    intersect_all,
    overridden_in_overlap,
    subtract,
)

K = ScheduleOverrideKind


class InvalidCode(StrEnum):
    """Why an instruction is rejected; the Spanish ``{reason}`` lives in the catalog."""

    MISSING_TIMES = "missing_times"
    END_BEFORE_START = "end_before_start"
    PAST_DATE = "past_date"
    ALREADY_OVER = "already_over"
    CLOSURE_OUTSIDE_HOURS = "closure_outside_hours"
    OPEN_OUT_OF_BOUNDS = "open_out_of_bounds"
    OPEN_INSIDE_BASE = "open_inside_base"


class ScheduleIntentInvalidError(Exception):
    def __init__(self, code: InvalidCode) -> None:
        super().__init__(code)
        self.code = code


class NotAScheduleInstructionError(Exception):
    """``kind`` is null: answer ``admin_help`` (CU-14 1a)."""


class NothingToReopenError(Exception):
    """``reopen`` with no closure in effect (CU-14 2b)."""

    def __init__(self, upcoming: list[OverrideView]) -> None:
        super().__init__("nothing to reopen")
        self.upcoming = upcoming


@dataclass(frozen=True)
class Proposal:
    """A valid instruction, ready to be stored as ``pending_confirmation``.

    ``starts_at`` is ``None`` for ``closed_today`` / ``closed_until_further_notice`` (set at
    confirmation, docs/05) and for ``reopen``.
    """

    kind: ScheduleOverrideKind
    business_date: dt.date | None
    starts_at: dt.datetime | None
    ends_at: dt.datetime | None
    reason: str | None
    to_lift: list[OverrideView] = field(default_factory=list)  # reopen: closures in effect
    upcoming: list[OverrideView] = field(default_factory=list)  # later closures of today
    replaced: list[OverrideView] = field(default_factory=list)  # overridden in the overlap

    def as_view(self, starts_at: dt.datetime | None = None) -> OverrideView:
        """``starts_at`` fills in the start of a closure that starts at confirmation."""
        return OverrideView(
            id=None,
            kind=self.kind,
            status=ScheduleOverrideStatus.ACTIVE,
            business_date=self.business_date,
            starts_at=self.starts_at or starts_at,
            ends_at=self.ends_at,
            reason=self.reason,
        )


def upcoming_closures(
    settings: Settings, overrides: Sequence[OverrideView], now: dt.datetime
) -> list[OverrideView]:
    """Applied closures starting later in the current business day (``{future_closures_note}``)."""
    today = business_date(now, settings.zoneinfo, settings.business_day_rollover_time)
    return [
        o
        for o in closures_touching(settings, overrides, today)
        if o.starts_at is not None and o.starts_at > now
    ]


def _clean_reason(reason: str | None) -> str | None:
    reason = (reason or "").strip().rstrip(".")
    return reason or None


def validate_schedule_intent(
    intent: ScheduleOverrideIntent,
    *,
    settings: Settings,
    now: dt.datetime,
    overrides: Sequence[OverrideView],
) -> Proposal:
    proposal = _validate(intent, settings=settings, now=now, overrides=overrides)
    if proposal.kind == K.REOPEN:
        return proposal
    # As if confirmed now: closures without a period start at the confirmation.
    replaced = overridden_in_overlap(overrides, proposal.as_view(starts_at=now), now)
    return replace(proposal, replaced=replaced)


def _validate(
    intent: ScheduleOverrideIntent,
    *,
    settings: Settings,
    now: dt.datetime,
    overrides: Sequence[OverrideView],
) -> Proposal:
    today = business_date(now, settings.zoneinfo, settings.business_day_rollover_time)
    reason = _clean_reason(intent.reason)
    if intent.kind is None:
        raise NotAScheduleInstructionError

    if intent.kind == K.CLOSED_UNTIL_FURTHER_NOTICE:
        return Proposal(intent.kind, None, None, None, reason)

    if intent.kind == K.REOPEN:
        to_lift = closures_in_effect(overrides, now)
        upcoming = upcoming_closures(settings, overrides, now)
        if not to_lift:
            raise NothingToReopenError(upcoming)
        return Proposal(intent.kind, None, None, None, None, to_lift=to_lift, upcoming=upcoming)

    if intent.kind == K.CLOSED_TODAY:
        end = day_bounds(settings, today).end
        remaining = intersect_all(
            effective_intervals(settings, overrides, today), Interval(now, end)
        )
        if not remaining:
            raise ScheduleIntentInvalidError(InvalidCode.CLOSURE_OUTSIDE_HOURS)
        return Proposal(intent.kind, today, None, end, reason)

    # closed_window / open_window
    if intent.start_time is None or intent.end_time is None:
        raise ScheduleIntentInvalidError(InvalidCode.MISSING_TIMES)
    day = intent.date or today
    if day < today:
        raise ScheduleIntentInvalidError(InvalidCode.PAST_DATE)
    window = Interval(
        at_local(settings, day, intent.start_time), at_local(settings, day, intent.end_time)
    )
    if window.is_empty:
        raise ScheduleIntentInvalidError(InvalidCode.END_BEFORE_START)
    if window.end <= now:
        raise ScheduleIntentInvalidError(InvalidCode.ALREADY_OVER)

    if intent.kind == K.CLOSED_WINDOW:
        # Clipped to the day's effective hours; empty = the closure changes nothing.
        parts = intersect_all(effective_intervals(settings, overrides, day), window)
        if not parts:
            raise ScheduleIntentInvalidError(InvalidCode.CLOSURE_OUTSIDE_HOURS)
        return Proposal(intent.kind, day, parts[0].start, parts[-1].end, reason)

    earliest = at_local(settings, day, settings.schedule_earliest_open_time)
    latest = at_local(settings, day, settings.schedule_latest_close_time)
    if window.start < earliest or window.end > latest:
        raise ScheduleIntentInvalidError(InvalidCode.OPEN_OUT_OF_BOUNDS)
    # It must open something that is not already open from now on (base hours, an earlier
    # opening): a later opening over a closure is valid (most recent wins).
    ahead = intersect_all([window], Interval(now, window.end))
    if not subtract(ahead, effective_intervals(settings, overrides, day)):
        raise ScheduleIntentInvalidError(InvalidCode.OPEN_INSIDE_BASE)
    return Proposal(intent.kind, day, window.start, window.end, reason)
