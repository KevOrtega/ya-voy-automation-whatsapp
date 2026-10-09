"""validate_schedule_intent (docs/05) and the Spanish summaries (doc 07)."""

from __future__ import annotations

import datetime as dt

import pytest

from tests.conftest import make_settings
from tests.schedule_helpers import D, K, at, override
from ya_voy.llm.schemas import ScheduleOverrideIntent
from ya_voy.messaging import catalog
from ya_voy.messaging import schedule_texts as texts
from ya_voy.schedule.hours import decide_acceptance, effective_intervals, report_due_at
from ya_voy.schedule.intents import (
    InvalidCode,
    NotAScheduleInstructionError,
    NothingToReopenError,
    ScheduleIntentInvalidError,
    validate_schedule_intent,
)

S = make_settings()
NOON = at(9, 12)
TOMORROW = D + dt.timedelta(days=1)


def intent(kind: K | None, **kw: object) -> ScheduleOverrideIntent:
    return ScheduleOverrideIntent(kind=kind, **kw)


def validate(i: ScheduleOverrideIntent, now: dt.datetime = NOON, overrides=()):
    return validate_schedule_intent(i, settings=S, now=now, overrides=list(overrides))


def invalid(i: ScheduleOverrideIntent, now: dt.datetime = NOON, overrides=()) -> InvalidCode:
    with pytest.raises(ScheduleIntentInvalidError) as info:
        validate(i, now, overrides)
    return info.value.code


def t(hour: int, minute: int = 0) -> dt.time:
    return dt.time(hour, minute)


# --- docs/05 examples --------------------------------------------------------------------------


def test_closed_today() -> None:
    p = validate(intent(K.CLOSED_TODAY, reason="lluvia"))
    assert (p.kind, p.business_date, p.starts_at, p.ends_at, p.reason) == (
        K.CLOSED_TODAY,
        D,
        None,  # set at confirmation
        at(10, 5),  # end of the business day
        "lluvia",
    )


def test_closed_until_further_notice() -> None:
    p = validate(intent(K.CLOSED_UNTIL_FURTHER_NOTICE))
    assert (p.business_date, p.starts_at, p.ends_at, p.reason) == (None, None, None, None)


def test_closed_window_today_by_default() -> None:
    p = validate(intent(K.CLOSED_WINDOW, start_time=t(15), end_time=t(18)))
    assert (p.business_date, p.starts_at, p.ends_at) == (D, at(9, 15), at(9, 18))


def test_open_window_until_midnight() -> None:
    p = validate(intent(K.OPEN_WINDOW, date=D, start_time=t(23), end_time=t(0)))
    assert (p.business_date, p.starts_at, p.ends_at) == (D, at(9, 23), at(10, 0))


def test_open_window_tomorrow_morning() -> None:
    p = validate(intent(K.OPEN_WINDOW, date=TOMORROW, start_time=t(8), end_time=t(9)))
    assert (p.business_date, p.starts_at, p.ends_at) == (TOMORROW, at(10, 8), at(10, 9))


def test_reopen_lists_closures_in_effect_and_upcoming() -> None:
    now_closed = override(K.CLOSED_TODAY, at(9, 11), at(10, 5), reason="lluvia", id=1)
    later = override(K.CLOSED_WINDOW, at(9, 20), at(9, 21), id=2)
    p = validate(intent(K.REOPEN), overrides=[now_closed, later])
    assert (p.to_lift, p.upcoming) == ([now_closed], [later])


def test_reopen_without_closures() -> None:
    later = override(K.CLOSED_WINDOW, at(9, 20), at(9, 21))
    with pytest.raises(NothingToReopenError) as info:
        validate(intent(K.REOPEN), overrides=[later])
    assert info.value.upcoming == [later]


def test_not_a_schedule_instruction() -> None:
    with pytest.raises(NotAScheduleInstructionError):
        validate(intent(None))


def test_reason_is_trimmed() -> None:
    assert validate(intent(K.CLOSED_TODAY, reason="  lluvia. ")).reason == "lluvia"
    assert validate(intent(K.CLOSED_TODAY, reason="  ")).reason is None


# --- invalid instructions ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("i", "code"),
    [
        (intent(K.CLOSED_WINDOW, start_time=t(15)), InvalidCode.MISSING_TIMES),
        (intent(K.OPEN_WINDOW, end_time=t(0)), InvalidCode.MISSING_TIMES),
        (intent(K.CLOSED_WINDOW, start_time=t(18), end_time=t(15)), InvalidCode.END_BEFORE_START),
        (intent(K.OPEN_WINDOW, start_time=t(1), end_time=t(23)), InvalidCode.END_BEFORE_START),
        (
            intent(K.CLOSED_WINDOW, date=D - dt.timedelta(1), start_time=t(15), end_time=t(18)),
            InvalidCode.PAST_DATE,
        ),
        (intent(K.CLOSED_WINDOW, start_time=t(10), end_time=t(11)), InvalidCode.ALREADY_OVER),
        (
            intent(K.CLOSED_WINDOW, start_time=t(23, 30), end_time=t(23, 50)),
            InvalidCode.CLOSURE_OUTSIDE_HOURS,
        ),
        (
            intent(K.OPEN_WINDOW, date=TOMORROW, start_time=t(5, 30), end_time=t(9)),
            InvalidCode.OPEN_OUT_OF_BOUNDS,
        ),
        (intent(K.OPEN_WINDOW, start_time=t(23), end_time=t(3)), InvalidCode.OPEN_OUT_OF_BOUNDS),
        (intent(K.OPEN_WINDOW, start_time=t(13), end_time=t(15)), InvalidCode.OPEN_INSIDE_BASE),
    ],
)
def test_invalid(i: ScheduleOverrideIntent, code: InvalidCode) -> None:
    assert invalid(i) == code


def test_opening_until_the_latest_close_is_allowed() -> None:
    p = validate(intent(K.OPEN_WINDOW, start_time=t(23), end_time=t(2)))
    assert p.ends_at == at(10, 2)


def test_opening_overlapping_the_base_is_allowed() -> None:
    p = validate(intent(K.OPEN_WINDOW, date=TOMORROW, start_time=t(8), end_time=t(10)))
    assert (p.starts_at, p.ends_at) == (at(10, 8), at(10, 10))


def test_closed_window_is_clipped_to_effective_hours() -> None:
    p = validate(intent(K.CLOSED_WINDOW, start_time=t(21), end_time=t(1)))
    assert (p.starts_at, p.ends_at) == (at(9, 21), at(9, 23))


def test_closed_window_inside_an_extension() -> None:
    until_one = override(K.OPEN_WINDOW, at(9, 23), at(10, 1))
    p = validate(
        intent(K.CLOSED_WINDOW, start_time=t(23, 30), end_time=t(2)), overrides=[until_one]
    )
    assert (p.starts_at, p.ends_at) == (at(9, 23, 30), at(10, 1))


def test_closed_today_after_hours_changes_nothing() -> None:
    assert invalid(intent(K.CLOSED_TODAY), now=at(9, 23, 30)) == InvalidCode.CLOSURE_OUTSIDE_HOURS
    until_one = override(K.OPEN_WINDOW, at(9, 23), at(10, 1))
    assert validate(intent(K.CLOSED_TODAY), at(9, 23, 30), [until_one]).ends_at == at(10, 5)


# --- most recent wins (RF-08, RN-25) -----------------------------------------------------------

RAIN = override(K.CLOSED_TODAY, at(9, 10), at(10, 5), reason="lluvia", confirmed_at=at(9, 10), id=1)


def test_opening_after_closed_today_is_valid_and_replaces_it() -> None:
    """The auditor's example: closed at 10:00, at 18:00 "abrimos de 19 a 23" (inside the base)."""
    p = validate(intent(K.OPEN_WINDOW, start_time=t(19), end_time=t(23)), at(9, 18), [RAIN])
    assert (p.starts_at, p.ends_at, p.replaced) == (at(9, 19), at(9, 23), [RAIN])


def test_opening_after_until_further_notice_is_valid_and_replaces_it() -> None:
    forever = override(K.CLOSED_UNTIL_FURTHER_NOTICE, at(8, 21), None, confirmed_at=at(8, 21), id=2)
    p = validate(intent(K.OPEN_WINDOW, start_time=t(19), end_time=t(1)), at(9, 18), [forever])
    assert (p.starts_at, p.ends_at, p.replaced) == (at(9, 19), at(10, 1), [forever])


def test_opening_of_hours_already_open_is_still_invalid() -> None:
    window = override(K.CLOSED_WINDOW, at(9, 15), at(9, 16), confirmed_at=at(9, 10), id=3)
    i = intent(K.OPEN_WINDOW, start_time=t(19), end_time=t(23))
    assert invalid(i, at(9, 18), [window]) == InvalidCode.OPEN_INSIDE_BASE
    # Only the part from now on counts: the closed 15-16 is already past at 18:00.
    early = intent(K.OPEN_WINDOW, start_time=t(15), end_time=t(19))
    assert invalid(early, at(9, 18), [window]) == InvalidCode.OPEN_INSIDE_BASE


def test_closures_replace_the_openings_they_overlap() -> None:
    opening = override(K.OPEN_WINDOW, at(9, 19), at(9, 23), confirmed_at=at(9, 18), id=4)
    late = override(K.OPEN_WINDOW, at(9, 23), at(10, 1), confirmed_at=at(9, 11), id=5)
    overrides = [RAIN, opening, late]
    window = validate(
        intent(K.CLOSED_WINDOW, start_time=t(20), end_time=t(21)), at(9, 19, 30), overrides
    )
    assert (window.starts_at, window.ends_at, window.replaced) == (at(9, 20), at(9, 21), [opening])
    today = validate(intent(K.CLOSED_TODAY), at(9, 19, 30), overrides)
    assert today.replaced == [late, opening]  # both confirmed after RAIN, in that order
    tomorrow = override(
        K.OPEN_WINDOW, at(10, 7), at(10, 9), day=TOMORROW, confirmed_at=at(9, 12), id=6
    )
    forever = validate(intent(K.CLOSED_UNTIL_FURTHER_NOTICE), NOON, [late, tomorrow])
    assert forever.replaced == [late, tomorrow]


def test_closed_window_over_hours_a_later_opening_reopened_is_valid() -> None:
    """Window clipping uses the current effective hours, which follow most-recent-wins."""
    opening = override(K.OPEN_WINDOW, at(9, 19), at(9, 23), confirmed_at=at(9, 18), id=4)
    i = intent(K.CLOSED_WINDOW, start_time=t(18), end_time=t(21))
    assert invalid(i, at(9, 17), [RAIN]) == InvalidCode.CLOSURE_OUTSIDE_HOURS
    p = validate(i, at(9, 18, 30), [RAIN, opening])
    assert (p.starts_at, p.ends_at) == (at(9, 19), at(9, 21))


def test_summary_of_an_opening_says_what_it_replaces() -> None:
    p = validate(intent(K.OPEN_WINDOW, start_time=t(19), end_time=t(23)), at(9, 18), [RAIN])
    view = p.as_view()
    summary = texts.override_summary(
        view,
        S,
        effective_hours_text=texts.effective_hours(effective_intervals(S, [RAIN, view], D), S),
        report_note_text=texts.report_note(
            None, window_end=at(9, 23), current_due_at=at(9, 23), settings=S
        ),
        replaced=p.replaced,
    )
    assert summary == (
        "El 09/10/2026 se toman pedidos también de 19:00 a 23:00. "
        "Horario de ese día: 09:00 a 10:00 y 19:00 a 23:00. "
        "Reemplaza, en el horario que se superpone: cierre de hoy por lluvia."
    )


def test_summary_of_a_closure_says_which_openings_it_replaces() -> None:
    late = override(K.OPEN_WINDOW, at(9, 23), at(10, 1), confirmed_at=at(9, 11), id=5)
    p = validate(intent(K.CLOSED_TODAY, reason="lluvia"), NOON, [late])
    assert texts.override_summary(p.as_view(starts_at=NOON), S, replaced=p.replaced) == (
        "Hoy 09/10/2026 no se toman más pedidos (motivo: lluvia). "
        "Reemplaza, en el horario que se superpone: apertura del 09/10/2026 de 23:00 a "
        "01:00 (del 10/10)."
    )
    both = [RAIN, override(K.CLOSED_WINDOW, at(9, 21), at(9, 22), confirmed_at=at(9, 11), id=7)]
    opening = validate(intent(K.OPEN_WINDOW, start_time=t(19), end_time=t(23)), at(9, 18), both)
    assert texts.replaced_note(opening.replaced, S) == (
        "Reemplaza, en el horario que se superpone: cierre de hoy por lluvia, "
        "cierre de 21:00 a 22:00."
    )


# --- rollover at 05:00 -------------------------------------------------------------------------


def test_instruction_just_before_rollover_refers_to_the_ending_day() -> None:
    """At 04:50 the business day is still 09/10: "de 15 a 18" refers to it and is over."""
    late = at(10, 4, 50)
    assert invalid(intent(K.CLOSED_WINDOW, start_time=t(15), end_time=t(18)), now=late) == (
        InvalidCode.ALREADY_OVER
    )
    early = at(10, 5, 10)
    p = validate(intent(K.CLOSED_WINDOW, start_time=t(15), end_time=t(18)), now=early)
    assert (p.business_date, p.starts_at) == (TOMORROW, at(10, 15))


def test_closed_today_just_before_and_after_rollover() -> None:
    assert invalid(intent(K.CLOSED_TODAY), now=at(10, 4, 50)) == InvalidCode.CLOSURE_OUTSIDE_HOURS
    p = validate(intent(K.CLOSED_TODAY), now=at(10, 5, 10))
    assert (p.business_date, p.ends_at) == (TOMORROW, at(11, 5))


def test_yesterday_is_past_after_rollover() -> None:
    i = intent(K.OPEN_WINDOW, date=D, start_time=t(23), end_time=t(0))
    assert invalid(i, now=at(10, 4, 59)) == InvalidCode.ALREADY_OVER  # still day D, but over
    assert invalid(i, now=at(10, 5, 0)) == InvalidCode.PAST_DATE


# --- Spanish texts (doc 07) --------------------------------------------------------------------


def test_open_window_summary_matches_the_doc_07_example() -> None:
    p = validate(intent(K.OPEN_WINDOW, start_time=t(23), end_time=t(0)))
    view = p.as_view()
    summary = texts.override_summary(
        view,
        S,
        effective_hours_text=texts.effective_hours(effective_intervals(S, [view], D), S),
        report_note_text=texts.report_note(
            None, window_end=view.ends_at, current_due_at=report_due_at(S, [], D), settings=S
        ),
    )
    assert summary == (
        "El 09/10/2026 se toman pedidos también de 23:00 a 00:00 (del 10/10). "
        "Horario de ese día: 09:00 a 00:00. El reporte del día se genera a las 00:00."
    )


def test_open_window_summary_when_the_report_was_sent() -> None:
    note = texts.report_note("sent", window_end=at(10, 1), current_due_at=at(9, 23), settings=S)
    assert note == (
        "El reporte de ese día ya se envió: los pedidos nuevos irán en una corrección a las 01:00."
    )


def test_open_window_summary_without_report_note() -> None:
    p = validate(intent(K.OPEN_WINDOW, date=TOMORROW, start_time=t(8), end_time=t(9)))
    view = p.as_view()
    note = texts.report_note(None, window_end=view.ends_at, current_due_at=at(10, 23), settings=S)
    summary = texts.override_summary(
        view, S, effective_hours_text="08:00 a 23:00", report_note_text=note
    )
    assert summary == (
        "El 10/10/2026 se toman pedidos también de 08:00 a 09:00. "
        "Horario de ese día: 08:00 a 23:00."
    )


def test_closure_summaries() -> None:
    today = validate(intent(K.CLOSED_TODAY, reason="lluvia")).as_view(starts_at=NOON)
    assert texts.override_summary(today, S) == (
        "Hoy 09/10/2026 no se toman más pedidos (motivo: lluvia)."
    )
    forever = validate(intent(K.CLOSED_UNTIL_FURTHER_NOTICE)).as_view(starts_at=NOON)
    assert texts.override_summary(forever, S) == (
        "Desde ahora no se toman pedidos hasta que me avises."
    )
    window = validate(intent(K.CLOSED_WINDOW, start_time=t(15), end_time=t(18))).as_view()
    assert (
        texts.override_summary(window, S) == "El 09/10/2026 de 15:00 a 18:00 no se toman pedidos."
    )


def test_reopen_summary_matches_doc_04() -> None:
    closed = override(K.CLOSED_TODAY, at(9, 11), at(10, 5), reason="lluvia")
    later = override(K.CLOSED_WINDOW, at(9, 20), at(9, 21))
    p = validate(intent(K.REOPEN), overrides=[closed, later])
    assert texts.override_summary(p.as_view(), S, to_lift=p.to_lift, upcoming=p.upcoming) == (
        "Vuelvo a tomar pedidos ahora. Se levanta: cierre de hoy por lluvia. "
        "Sigue programado: cierre de 20:00 a 21:00."
    )


def test_confirm_message() -> None:
    summary = texts.override_summary(
        validate(intent(K.CLOSED_TODAY, reason="lluvia")).as_view(starts_at=NOON), S
    )
    assert catalog.render(catalog.ADMIN_SCHEDULE_CONFIRM, override_summary=summary, minutes=5) == (
        "Entendí esto:\nHoy 09/10/2026 no se toman más pedidos (motivo: lluvia).\n\n"
        "¿Lo aplico? Respondé *sí* o *no* (tenés 5 minutos)."
    )


def test_status_line() -> None:
    closed = override(K.CLOSED_TODAY, at(9, 11), at(10, 5), reason="lluvia")
    assert texts.status_line(decide_acceptance(S, [closed], NOON)) == "Ahora: cerrado (lluvia)"
    assert texts.status_line(decide_acceptance(S, [], NOON)) == "Ahora: tomando pedidos"
    late = decide_acceptance(S, [], at(9, 23, 30))
    assert texts.status_line(late) == "Ahora: cerrado (fuera de horario)"
    no_reason = override(K.CLOSED_WINDOW, at(9, 11), at(9, 13))
    assert texts.status_line(decide_acceptance(S, [no_reason], NOON)) == "Ahora: cerrado"


def test_invalid_reasons_from_doc_07() -> None:
    assert texts.invalid_reason("open_out_of_bounds", S) == (
        "la apertura tiene que quedar entre las 06:00 y las 02:00 del día siguiente"
    )
    assert texts.invalid_reason("open_inside_base", S) == (
        "ese horario ya está dentro del horario normal"
    )
    assert texts.invalid_reason("end_before_start", S) == (
        "la hora de fin tiene que ser posterior a la de inicio"
    )
    assert set(catalog.SCHEDULE_INVALID_REASONS) == {c.value for c in InvalidCode}


@pytest.mark.parametrize(
    ("closure", "key", "body"),
    [
        (
            override(K.CLOSED_TODAY, at(9, 11), at(10, 5), reason="lluvia"),
            catalog.STORE_CLOSED_TODAY,
            "🚫 Hoy no tomamos pedidos por lluvia.",
        ),
        (
            override(K.CLOSED_UNTIL_FURTHER_NOTICE, at(9, 11), None),
            catalog.STORE_CLOSED_UNTIL_FURTHER_NOTICE,
            "🚫 Por el momento no estamos tomando pedidos. Probá de nuevo más tarde.",
        ),
        (
            override(K.CLOSED_WINDOW, at(9, 11), at(9, 13)),
            catalog.STORE_CLOSED_WINDOW,
            "🚫 Hoy de 11:00 a 13:00 no tomamos pedidos. "
            "Escribinos de nuevo a partir de las 13:00.",
        ),
    ],
)
def test_store_closed_replies(closure, key: str, body: str) -> None:
    decision = decide_acceptance(S, [closure], NOON)
    assert texts.store_closed_reply(decision, S, []) == (key, body)


def test_store_out_of_hours_reply_shows_today_hours() -> None:
    until_midnight = override(K.OPEN_WINDOW, at(9, 23), at(10, 0))
    decision = decide_acceptance(S, [until_midnight], at(9, 8))
    hours = effective_intervals(S, [until_midnight], D)
    assert texts.store_closed_reply(decision, S, hours) == (
        catalog.STORE_OUT_OF_HOURS,
        "🌙 Estamos fuera de horario. Hoy tomamos pedidos de 09:00 a 00:00. "
        "Mandalo de nuevo cuando abramos.",
    )
