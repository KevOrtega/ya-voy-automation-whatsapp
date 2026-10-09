"""Effective schedule: base hours, openings, closures, business day rollover (RF-07..RF-09)."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from tests.conftest import make_settings
from tests.schedule_helpers import D, K, at, override
from ya_voy.domain.enums import ScheduleOverrideStatus
from ya_voy.messaging.schedule_texts import effective_hours
from ya_voy.schedule.calendar import business_date, business_day_bounds, local_at
from ya_voy.schedule.hours import (
    AcceptanceReason,
    decide_acceptance,
    effective_intervals,
    report_due_at,
)

S = make_settings()


def decide(when: dt.datetime, *overrides):
    return decide_acceptance(S, overrides, when)


def hours(*overrides, day: dt.date = D) -> str:
    return effective_hours(effective_intervals(S, overrides, day), S)


# --- base hours ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("when", "accepting"),
    [
        (at(9, 8, 59), False),
        (at(9, 9, 0), True),
        (at(9, 15, 0), True),
        (at(9, 22, 59), True),
        (at(9, 23, 0), False),
        (at(10, 0, 30), False),
    ],
)
def test_base_hours(when: dt.datetime, accepting: bool) -> None:
    decision = decide(when)
    assert decision.accepting is accepting
    assert decision.reason == (
        AcceptanceReason.OPEN if accepting else AcceptanceReason.OUTSIDE_HOURS
    )
    assert decision.closure is None


def test_plain_day() -> None:
    assert hours() == "09:00 a 23:00"
    assert report_due_at(S, [], D) == at(9, 23)


# --- business day rollover at 05:00 ------------------------------------------------------------


@pytest.mark.parametrize(
    ("when", "day"),
    [
        (at(9, 5, 0), dt.date(2026, 10, 9)),
        (at(10, 0, 0), dt.date(2026, 10, 9)),
        (at(10, 4, 59), dt.date(2026, 10, 9)),
        (at(10, 5, 0), dt.date(2026, 10, 10)),
    ],
)
def test_business_date_rolls_over_at_five(when: dt.datetime, day: dt.date) -> None:
    assert decide(when).business_date == day


def test_closed_today_ends_at_the_rollover() -> None:
    closed = override(K.CLOSED_TODAY, at(9, 12), at(10, 5), reason="lluvia")
    assert decide(at(10, 4, 59), closed).reason == AcceptanceReason.CLOSED
    after = decide(at(10, 5, 0), closed)  # next business day: no longer closed, before 09:00
    assert (after.reason, after.business_date) == (
        AcceptanceReason.OUTSIDE_HOURS,
        D + dt.timedelta(1),
    )
    assert decide(at(10, 9, 0), closed).accepting


def test_local_at_puts_early_hours_on_the_next_day() -> None:
    rollover = dt.time(5)
    ba = ZoneInfo("America/Argentina/Buenos_Aires")
    assert local_at(D, dt.time(0, 0), ba, rollover) == at(10, 0)
    assert local_at(D, dt.time(4, 59), ba, rollover) == at(10, 4, 59)
    assert local_at(D, dt.time(5, 0), ba, rollover) == at(9, 5)
    assert local_at(D, dt.time(23, 0), ba, rollover) == at(9, 23)


def test_business_day_spans_24_hours_without_dst() -> None:
    start, end = business_day_bounds(D, ZoneInfo("America/Argentina/Buenos_Aires"), dt.time(5))
    assert end - start == dt.timedelta(hours=24)


@pytest.mark.parametrize(
    ("day", "length"),
    [(dt.date(2026, 3, 28), 23), (dt.date(2026, 10, 24), 25), (dt.date(2026, 6, 1), 24)],
)
def test_business_day_is_dst_safe(day: dt.date, length: int) -> None:
    """Europe/Madrid jumps 02:00->03:00 on 29/03 and 03:00->02:00 on 25/10 (inside the night)."""
    madrid = ZoneInfo("Europe/Madrid")
    start, end = business_day_bounds(day, madrid, dt.time(5))
    assert (end - start) == dt.timedelta(hours=length)
    # 04:30 local after the change still belongs to the previous business day; 05:00 does not.
    next_day = day + dt.timedelta(days=1)
    before = dt.datetime.combine(next_day, dt.time(4, 30), tzinfo=madrid)
    after = dt.datetime.combine(next_day, dt.time(5, 0), tzinfo=madrid)
    assert business_date(before, madrid, dt.time(5)) == day
    assert business_date(after, madrid, dt.time(5)) == next_day


def test_dst_zone_schedule() -> None:
    madrid = make_settings(tz="Europe/Madrid")
    day = dt.date(2026, 3, 28)
    opening = override(
        K.OPEN_WINDOW,
        dt.datetime(2026, 3, 28, 23, tzinfo=ZoneInfo("Europe/Madrid")),
        dt.datetime(2026, 3, 29, 1, tzinfo=ZoneInfo("Europe/Madrid")),
        day=day,
    )
    intervals = effective_intervals(madrid, [opening], day)
    assert effective_hours(intervals, madrid) == "09:00 a 01:00"
    inside = dt.datetime(2026, 3, 29, 0, 30, tzinfo=ZoneInfo("Europe/Madrid"))
    assert decide_acceptance(madrid, [opening], inside).business_date == day


# --- extensions past midnight ------------------------------------------------------------------


def test_extension_past_midnight_belongs_to_the_extended_day() -> None:
    until_one = override(K.OPEN_WINDOW, at(9, 23), at(10, 1))
    late = decide(at(10, 0, 30), until_one)
    assert (late.accepting, late.business_date) == (True, D)
    assert not decide(at(10, 1, 0), until_one).accepting
    assert hours(until_one) == "09:00 a 01:00"
    assert report_due_at(S, [until_one], D) == at(10, 1)


def test_morning_opening() -> None:
    early = override(K.OPEN_WINDOW, at(9, 8), at(9, 9))
    assert decide(at(9, 8, 30), early).accepting
    assert hours(early) == "08:00 a 23:00"
    assert report_due_at(S, [early], D) == at(9, 23)  # does not move the report


def test_opening_of_another_day_does_not_count() -> None:
    tomorrow = override(K.OPEN_WINDOW, at(10, 8), at(10, 9), day=D + dt.timedelta(1))
    assert hours(tomorrow) == "09:00 a 23:00"
    assert report_due_at(S, [tomorrow], D) == at(9, 23)


# --- closures and precedence -------------------------------------------------------------------


def test_closed_window_cuts_the_day() -> None:
    window = override(K.CLOSED_WINDOW, at(9, 15), at(9, 18))
    closed = decide(at(9, 16), window)
    assert (closed.reason, closed.closure) == (AcceptanceReason.CLOSED, window)
    assert decide(at(9, 18), window).accepting
    assert hours(window) == "09:00 a 15:00 y 18:00 a 23:00"


def test_closure_wins_over_an_overlapping_opening() -> None:
    """RF-08 / RN-25: where a closure and an opening overlap, the closure wins."""
    opening = override(K.OPEN_WINDOW, at(9, 23), at(10, 1))
    closure = override(K.CLOSED_WINDOW, at(9, 22), at(10, 0, 30))
    assert decide(at(9, 23, 30), opening, closure).reason == AcceptanceReason.CLOSED
    assert decide(at(10, 0, 15), opening, closure).reason == AcceptanceReason.CLOSED
    assert decide(at(10, 0, 45), opening, closure).accepting
    assert hours(opening, closure) == "09:00 a 22:00 y 00:30 a 01:00"
    assert report_due_at(S, [opening, closure], D) == at(10, 1)  # closures never move it


def test_closed_today_also_closes_the_openings_of_the_day() -> None:
    """RN-26: closed_today runs to the end of the business day, extra openings included."""
    opening = override(K.OPEN_WINDOW, at(9, 23), at(10, 1))
    closed = override(K.CLOSED_TODAY, at(9, 20), at(10, 5), reason="lluvia")
    assert decide(at(10, 0, 30), opening, closed).closure == closed
    assert hours(opening, closed) == "09:00 a 20:00"


def test_widest_closure_is_reported() -> None:
    window = override(K.CLOSED_WINDOW, at(9, 15), at(9, 18))
    today = override(K.CLOSED_TODAY, at(9, 14), at(10, 5))
    forever = override(K.CLOSED_UNTIL_FURTHER_NOTICE, at(9, 13), None)
    assert decide(at(9, 16), window, today).closure == today
    assert decide(at(9, 16), window, today, forever).closure == forever


def test_until_further_notice_spans_days() -> None:
    forever = override(K.CLOSED_UNTIL_FURTHER_NOTICE, at(9, 13), None)
    assert decide(at(12, 12), forever).closure == forever
    assert hours(forever) == "09:00 a 13:00"
    assert hours(forever, day=D + dt.timedelta(1)) == "cerrado todo el día"


@pytest.mark.parametrize(
    "status",
    [
        ScheduleOverrideStatus.CANCELLED,
        ScheduleOverrideStatus.PENDING_CONFIRMATION,
        ScheduleOverrideStatus.REJECTED,
        ScheduleOverrideStatus.EXPIRED,
        ScheduleOverrideStatus.SUPERSEDED,
    ],
)
def test_only_active_overrides_count(status: ScheduleOverrideStatus) -> None:
    closure = override(K.CLOSED_WINDOW, at(9, 15), at(9, 18), status=status)
    opening = override(K.OPEN_WINDOW, at(9, 23), at(10, 1), status=status)
    assert decide(at(9, 16), closure, opening).accepting
    assert not decide(at(9, 23, 30), closure, opening).accepting
    assert hours(closure, opening) == "09:00 a 23:00"


def test_lifted_closure_is_ignored() -> None:
    lifted = override(K.CLOSED_TODAY, at(9, 12), at(10, 5), cancelled_at=at(9, 13))
    assert decide(at(9, 14), lifted).accepting


def test_ended_closure_no_longer_applies() -> None:
    window = override(K.CLOSED_WINDOW, at(9, 10), at(9, 11))
    assert decide(at(9, 11), window).accepting
