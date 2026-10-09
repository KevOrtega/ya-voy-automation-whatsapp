"""Schedule variables of doc 07 (``{override_summary}``, ``{effective_hours}``, ...).

Dates are ``dd/mm/aaaa`` and hours ``HH:MM`` in 24 h, in ``TZ``; an hour after midnight of a
business day gets ``" (del dd/mm)"`` (doc 07 ``{window_start}``, ``{window_end}``).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

from ya_voy.config import Settings
from ya_voy.domain.enums import DailyReportStatus, ScheduleOverrideKind
from ya_voy.messaging import catalog
from ya_voy.schedule.hours import AcceptanceDecision, AcceptanceReason, Interval, OverrideView

K = ScheduleOverrideKind
# The report of that day was already sent (or is being sent): the opening reopens it (RF-55).
REPORT_ALREADY_OUT = frozenset(
    {DailyReportStatus.SENT, DailyReportStatus.GENERATING, DailyReportStatus.CORRECTION_PENDING}
)


def fmt_date(day: dt.date) -> str:
    return day.strftime("%d/%m/%Y")


def fmt_hhmm(at: dt.datetime, settings: Settings) -> str:
    return at.astimezone(settings.zoneinfo).strftime("%H:%M")


def fmt_time(at: dt.datetime, day: dt.date, settings: Settings) -> str:
    """``HH:MM``, plus ``" (del dd/mm)"`` when it falls after the calendar date of ``day``."""
    local = at.astimezone(settings.zoneinfo)
    text = local.strftime("%H:%M")
    if local.date() > day:
        text += catalog.NEXT_DAY_SUFFIX.format(date=local.strftime("%d/%m"))
    return text


def effective_hours(intervals: Sequence[Interval], settings: Settings) -> str:
    """``"09:00 a 23:00"``; several ranges ``"09:00 a 15:00 y 18:00 a 23:00"``."""
    parts = [f"{fmt_hhmm(i.start, settings)} a {fmt_hhmm(i.end, settings)}" for i in intervals]
    if not parts:
        return catalog.NO_EFFECTIVE_HOURS
    if len(parts) == 1:
        return parts[0]
    return f"{', '.join(parts[:-1])} y {parts[-1]}"


def reason_paren(reason: str | None) -> str:
    return catalog.REASON_PAREN.format(reason=reason) if reason else ""


def reason_text(reason: str | None) -> str:
    return catalog.REASON_TEXT.format(reason=reason) if reason else ""


def _window(o: OverrideView, settings: Settings) -> dict[str, str]:
    assert o.starts_at is not None and o.ends_at is not None and o.business_date is not None
    return {
        "window_start": fmt_time(o.starts_at, o.business_date, settings),
        "window_end": fmt_time(o.ends_at, o.business_date, settings),
    }


def closure_item(o: OverrideView, settings: Settings) -> str:
    template = catalog.CLOSURE_ITEMS[o.kind]
    window = _window(o, settings) if o.kind == K.CLOSED_WINDOW else {}
    return template.format(reason_text=reason_text(o.reason), **window)


def closures_list(closures: Sequence[OverrideView], settings: Settings) -> str:
    return ", ".join(closure_item(o, settings) for o in closures)


def future_closures_note(upcoming: Sequence[OverrideView], settings: Settings) -> str:
    if not upcoming:
        return ""
    return catalog.FUTURE_CLOSURES_NOTE.format(items=closures_list(upcoming, settings))


def accepting_label(decision: AcceptanceDecision) -> str:
    if decision.accepting:
        return catalog.ACCEPTING_LABEL_OPEN
    if decision.reason == AcceptanceReason.OUTSIDE_HOURS:
        return catalog.ACCEPTING_LABEL_CLOSED.format(reason=catalog.OUTSIDE_HOURS_REASON)
    assert decision.closure is not None
    if decision.closure.reason:
        return catalog.ACCEPTING_LABEL_CLOSED.format(reason=decision.closure.reason)
    return catalog.ACCEPTING_LABEL_CLOSED_NO_REASON


def status_line(decision: AcceptanceDecision) -> str:
    return catalog.STATUS_LINE.format(accepting_label=accepting_label(decision))


def report_note(
    report_status: str | None,
    *,
    window_end: dt.datetime,
    current_due_at: dt.datetime,
    settings: Settings,
) -> str:
    """``{report_note}`` of an ``open_window`` summary (doc 07)."""
    end = fmt_hhmm(window_end, settings)
    if report_status in REPORT_ALREADY_OUT:
        return catalog.REPORT_NOTE_REOPENED.format(window_end=end)
    if window_end > current_due_at:
        return catalog.REPORT_NOTE_MOVED.format(window_end=end)
    return ""


def override_summary(
    o: OverrideView,
    settings: Settings,
    *,
    effective_hours_text: str = "",
    report_note_text: str = "",
    to_lift: Sequence[OverrideView] = (),
    upcoming: Sequence[OverrideView] = (),
) -> str:
    template = catalog.OVERRIDE_SUMMARIES[o.kind]
    if o.kind == K.CLOSED_TODAY:
        assert o.business_date is not None
        text = template.format(date=fmt_date(o.business_date), reason_paren=reason_paren(o.reason))
    elif o.kind == K.CLOSED_UNTIL_FURTHER_NOTICE:
        text = template.format(reason_paren=reason_paren(o.reason))
    elif o.kind == K.CLOSED_WINDOW:
        assert o.business_date is not None
        text = template.format(
            date=fmt_date(o.business_date),
            reason_paren=reason_paren(o.reason),
            **_window(o, settings),
        )
    elif o.kind == K.OPEN_WINDOW:
        assert o.business_date is not None
        text = template.format(
            date=fmt_date(o.business_date),
            reason_paren=reason_paren(o.reason),
            effective_hours=effective_hours_text,
            report_note=report_note_text,
            **_window(o, settings),
        )
    else:
        text = template.format(
            closures_list=closures_list(to_lift, settings),
            future_closures_note=future_closures_note(upcoming, settings),
        )
    return text.strip()


def invalid_reason(code: str, settings: Settings) -> str:
    return catalog.SCHEDULE_INVALID_REASONS[code].format(
        earliest=settings.schedule_earliest_open_time.strftime("%H:%M"),
        latest=settings.schedule_latest_close_time.strftime("%H:%M"),
    )


def store_closed_reply(
    decision: AcceptanceDecision, settings: Settings, today_hours: Sequence[Interval]
) -> tuple[str, str]:
    """``(template_key, body)`` for a store writing while not accepting orders (CU-10 step 3).

    For stage 4b; the cooldown (``OUT_OF_HOURS_REPLY_COOLDOWN_MINUTES``) is the caller's.
    """
    assert not decision.accepting
    closure = decision.closure
    if closure is None:
        key = catalog.STORE_OUT_OF_HOURS
        return key, catalog.render(key, effective_hours=effective_hours(today_hours, settings))
    reason = reason_text(closure.reason)
    if closure.kind == K.CLOSED_TODAY:
        return catalog.STORE_CLOSED_TODAY, catalog.render(
            catalog.STORE_CLOSED_TODAY, reason_text=reason
        )
    if closure.kind == K.CLOSED_UNTIL_FURTHER_NOTICE:
        key = catalog.STORE_CLOSED_UNTIL_FURTHER_NOTICE
        return key, catalog.render(key, reason_text=reason)
    key = catalog.STORE_CLOSED_WINDOW
    return key, catalog.render(key, reason_text=reason, **_window(closure, settings))
