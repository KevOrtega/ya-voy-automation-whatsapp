"""Admin schedule changes against Postgres/Redis (CU-14, RF-41..RF-46, RN-24..RN-28).

"Now" is frozen through ``schedule_admin.db_now`` so the scenarios do not depend on the hour
the suite runs; the sweeps use the same clock.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import itertools
import time
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.integration import factories as f
from tests.integration.conftest import queued
from tests.schedule_helpers import at
from ya_voy.db.models import DailyReport, OutboundMessage, ScheduleOverride
from ya_voy.domain.enums import ScheduleOverrideKind as K
from ya_voy.llm.extractor import LLMError, ScheduleContext
from ya_voy.llm.schemas import ScheduleOverrideIntent
from ya_voy.locks import admin_lock_key
from ya_voy.messaging import catalog
from ya_voy.messaging.schedule_texts import effective_hours
from ya_voy.queue import SEND_OUTBOUND
from ya_voy.schedule import repository
from ya_voy.schedule.hours import AcceptanceReason
from ya_voy.worker import schedule_admin, schedules
from ya_voy.worker.schedule_admin import AdminText

Ctx = Callable[..., dict[str, Any]]
SF = async_sessionmaker[AsyncSession]
ADMIN = "5491100000001"
OTHER_ADMIN = "5491100000002"
D = dt.date(2026, 10, 9)
_ids = itertools.count(1)
APPLIED = catalog.ADMIN_SCHEDULE_APPLIED
STILL_CLOSED = catalog.ADMIN_SCHEDULE_STILL_CLOSED

RAIN = "hoy no tomamos pedidos por lluvia"
UNTIL_NOTICE = "hoy no tomamos pedidos hasta nuevo aviso"
WINDOW = "de 15 a 18 no tomaremos pedidos hoy"
MIDNIGHT = "hoy hasta las 00"
REOPEN = "ya tomamos pedidos de nuevo"
QUESTION = "¿cuántos pedidos hubo hoy?"
BAD_OPENING = "hoy de 13 a 15 abrimos más"
UNTIL_ONE = "hoy hasta la 01"
EVENING = "abrimos de 19 a 23"
INTENTS = {
    RAIN: ScheduleOverrideIntent(kind=K.CLOSED_TODAY, reason="lluvia"),
    UNTIL_NOTICE: ScheduleOverrideIntent(kind=K.CLOSED_UNTIL_FURTHER_NOTICE),
    WINDOW: ScheduleOverrideIntent(
        kind=K.CLOSED_WINDOW, start_time=dt.time(15), end_time=dt.time(18)
    ),
    MIDNIGHT: ScheduleOverrideIntent(
        kind=K.OPEN_WINDOW, date=D, start_time=dt.time(23), end_time=dt.time(0)
    ),
    REOPEN: ScheduleOverrideIntent(kind=K.REOPEN),
    BAD_OPENING: ScheduleOverrideIntent(
        kind=K.OPEN_WINDOW, start_time=dt.time(13), end_time=dt.time(15)
    ),
    UNTIL_ONE: ScheduleOverrideIntent(
        kind=K.OPEN_WINDOW, start_time=dt.time(23), end_time=dt.time(1)
    ),
    EVENING: ScheduleOverrideIntent(
        kind=K.OPEN_WINDOW, start_time=dt.time(19), end_time=dt.time(23)
    ),
}


class Clock:
    def __init__(self, now: dt.datetime) -> None:
        self.now = now

    async def __call__(self, session: AsyncSession) -> dt.datetime:
        return self.now


class StubParser:
    def __init__(self, *, fail: bool = False, delay: float = 0.0) -> None:
        self.fail = fail
        self.delay = delay
        self.calls: list[tuple[str, ScheduleContext]] = []
        self.running = 0
        self.max_running = 0

    async def parse(self, text: str, context: ScheduleContext) -> ScheduleOverrideIntent:
        self.calls.append((text, context))
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.fail:
                raise LLMError("stub outage")
            return INTENTS.get(text, ScheduleOverrideIntent())
        finally:
            self.running -= 1


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock(at(9, 12))
    monkeypatch.setattr(schedule_admin, "db_now", c)
    monkeypatch.setattr(repository, "db_now", c)
    return c


@pytest.fixture
def ctx(worker_ctx: Ctx, clock: Clock) -> dict[str, Any]:
    c = worker_ctx()
    c["schedule_parser"] = StubParser()
    return c


def admin_text(body: str, admin: str = ADMIN) -> AdminText:
    return AdminText(
        admin_phone=admin,
        reply_jid=f"{admin}@s.whatsapp.net",
        text=body,
        wa_message_id=f"WAM-admin-{next(_ids)}",
    )


async def say(ctx: dict[str, Any], body: str, admin: str = ADMIN) -> schedule_admin.AdminResult:
    result = await schedules.handle_admin_text(ctx, admin_text(body, admin))
    await schedules.after_commit(ctx, result)
    return result


async def overrides(sf: SF) -> list[ScheduleOverride]:
    async with sf() as session:
        return list(await session.scalars(select(ScheduleOverride).order_by(ScheduleOverride.id)))


async def bodies(sf: SF, ids: list[int]) -> list[str]:
    async with sf() as session:
        rows = await session.scalars(
            select(OutboundMessage).where(OutboundMessage.id.in_(ids)).order_by(OutboundMessage.id)
        )
        return [r.body for r in rows]


async def outbox(sf: SF) -> list[OutboundMessage]:
    async with sf() as session:
        return list(await session.scalars(select(OutboundMessage).order_by(OutboundMessage.id)))


async def accepting(ctx: dict[str, Any], when: dt.datetime) -> Any:
    async with ctx["session_factory"]() as session:
        return await repository.is_accepting_orders(session, ctx["settings"], when)


# --- closed_today, the main flow --------------------------------------------------------------


async def test_closed_today_request_confirm_and_effect(ctx: dict[str, Any], clock: Clock) -> None:
    sf = ctx["session_factory"]
    request = await say(ctx, RAIN)

    assert request.outcome == "confirmation_requested"
    [row] = await overrides(sf)
    assert (row.kind, row.status, row.business_date, row.reason) == (
        "closed_today",
        "pending_confirmation",
        D,
        "lluvia",
    )
    assert (row.starts_at, row.ends_at) == (None, at(10, 5))
    assert row.confirmation_expires_at == at(9, 12, 5)
    assert (row.source_text, row.created_by) == (RAIN, ADMIN)
    assert row.parsed == {
        "kind": "closed_today",
        "date": None,
        "start_time": None,
        "end_time": None,
        "reason": "lluvia",
    }
    [confirm] = await outbox(sf)
    assert (confirm.template_key, confirm.recipient_jid) == (
        catalog.ADMIN_SCHEDULE_CONFIRM,
        f"{ADMIN}@s.whatsapp.net",
    )
    assert confirm.idempotency_key == f"schedule_override:{row.id}:confirm"
    assert confirm.body == (
        "Entendí esto:\nHoy 09/10/2026 no se toman más pedidos (motivo: lluvia).\n\n"
        "¿Lo aplico? Respondé *sí* o *no* (tenés 5 minutos)."
    )
    assert (SEND_OUTBOUND, (confirm.id,)) in await queued(ctx["redis"], SEND_OUTBOUND)
    assert (await accepting(ctx, at(9, 12, 1))).accepting  # not applied before the "sí"

    clock.now = at(9, 12, 3)
    applied = await say(ctx, "sí")

    assert applied.outcome == "applied"
    [row] = await overrides(sf)
    assert (row.status, row.starts_at, row.confirmed_at, row.ends_at) == (
        "active",
        at(9, 12, 3),
        at(9, 12, 3),
        at(10, 5),
    )
    assert await bodies(sf, applied.outbound_ids) == [
        "✅ Listo. Hoy 09/10/2026 no se toman más pedidos (motivo: lluvia).\n"
        "Ahora: cerrado (lluvia)"
    ]
    decision = await accepting(ctx, at(9, 15))
    assert (decision.reason, decision.closure.reason) == (AcceptanceReason.CLOSED, "lluvia")
    assert (await accepting(ctx, at(10, 5))).reason == AcceptanceReason.OUTSIDE_HOURS
    assert (await accepting(ctx, at(10, 9))).accepting


async def test_no_rejects(ctx: dict[str, Any]) -> None:
    await say(ctx, WINDOW)
    result = await say(ctx, "no")
    assert result.outcome == "rejected"
    [row] = await overrides(ctx["session_factory"])
    assert row.status == "rejected"
    assert await bodies(ctx["session_factory"], result.outbound_ids) == ["Ok, no cambié nada."]
    assert (await accepting(ctx, at(9, 16))).accepting


async def test_new_instruction_supersedes_the_pending_one(ctx: dict[str, Any]) -> None:
    await say(ctx, WINDOW)
    await say(ctx, RAIN)
    rows = await overrides(ctx["session_factory"])
    assert [(r.kind, r.status) for r in rows] == [
        ("closed_window", "superseded"),
        ("closed_today", "pending_confirmation"),
    ]
    await say(ctx, "sí")
    assert [r.status for r in await overrides(ctx["session_factory"])] == ["superseded", "active"]


async def test_admins_have_independent_requests(ctx: dict[str, Any]) -> None:
    await say(ctx, WINDOW, ADMIN)
    await say(ctx, RAIN, OTHER_ADMIN)
    assert [r.status for r in await overrides(ctx["session_factory"])] == [
        "pending_confirmation",
        "pending_confirmation",
    ]


# --- answers without a request -----------------------------------------------------------------


async def test_invalid_instruction_keeps_the_pending_one(ctx: dict[str, Any]) -> None:
    await say(ctx, WINDOW)
    result = await say(ctx, BAD_OPENING)
    assert result.outcome == "invalid:open_inside_base"
    assert await bodies(ctx["session_factory"], result.outbound_ids) == [
        "No pude aplicar eso: ese horario ya está dentro del horario normal. Probá de nuevo, "
        "por ejemplo: _de 15 a 18 no tomamos pedidos_ o _hoy hasta las 00_."
    ]
    assert [r.status for r in await overrides(ctx["session_factory"])] == ["pending_confirmation"]


async def test_not_a_schedule_instruction_gets_help(ctx: dict[str, Any]) -> None:
    result = await say(ctx, QUESTION)
    assert result.outcome == "not_schedule"
    [body] = await bodies(ctx["session_factory"], result.outbound_ids)
    assert body == catalog.render(catalog.ADMIN_HELP)
    assert "*cancelar {código}*" in body
    assert await overrides(ctx["session_factory"]) == []


async def test_llm_failure(ctx: dict[str, Any]) -> None:
    ctx["schedule_parser"] = StubParser(fail=True)
    result = await say(ctx, RAIN)
    assert result.outcome == "parse_error"
    assert await bodies(ctx["session_factory"], result.outbound_ids) == [
        "No pude interpretar el mensaje en este momento. Probá de nuevo en unos minutos."
    ]
    assert await overrides(ctx["session_factory"]) == []


async def test_yes_without_request_gets_help(ctx: dict[str, Any]) -> None:
    result = await say(ctx, "sí")
    assert result.outcome == "nothing_pending"
    assert await bodies(ctx["session_factory"], result.outbound_ids) == [
        catalog.render(catalog.ADMIN_HELP)
    ]


async def test_commands_left_for_stage_7b(ctx: dict[str, Any]) -> None:
    for body in ("estado", "cancelar YV-0001", "liberar YV-0001"):
        result = await say(ctx, body)
        assert result.outcome.startswith("command:") and result.outbound_ids == []
    assert ctx["schedule_parser"].calls == []


# --- reopen ------------------------------------------------------------------------------------


async def test_reopen_lifts_closures_in_effect_only(ctx: dict[str, Any], clock: Clock) -> None:
    sf = ctx["session_factory"]
    await say(ctx, WINDOW, OTHER_ADMIN)  # a later closure: must survive the reopen
    assert (await say(ctx, "sí", OTHER_ADMIN)).outcome == "applied"
    clock.now = at(9, 13)
    await say(ctx, RAIN)
    assert (await say(ctx, "sí")).outcome == "applied"
    assert (await accepting(ctx, at(9, 14))).reason == AcceptanceReason.CLOSED
    clock.now = at(9, 13, 30)

    request = await say(ctx, REOPEN)
    assert await bodies(sf, request.outbound_ids) == [
        "Entendí esto:\nVuelvo a tomar pedidos ahora. Se levanta: cierre de hoy por lluvia. "
        "Sigue programado: cierre de 15:00 a 18:00.\n\n"
        "¿Lo aplico? Respondé *sí* o *no* (tenés 5 minutos)."
    ]
    applied = await say(ctx, "sí")
    assert await bodies(sf, applied.outbound_ids) == [
        "✅ Listo. Vuelvo a tomar pedidos ahora. Se levanta: cierre de hoy por lluvia. "
        "Sigue programado: cierre de 15:00 a 18:00.\nAhora: tomando pedidos"
    ]
    rows = await overrides(sf)
    by_kind = {r.kind: r for r in rows}
    assert by_kind["reopen"].status == "applied"
    assert (by_kind["closed_today"].status, by_kind["closed_today"].cancelled_by_override_id) == (
        "cancelled",
        by_kind["reopen"].id,
    )
    assert by_kind["closed_window"].status == "active"
    assert (await accepting(ctx, at(9, 14))).accepting
    assert (await accepting(ctx, at(9, 16))).reason == AcceptanceReason.CLOSED


async def test_reopen_without_closures(ctx: dict[str, Any]) -> None:
    result = await say(ctx, REOPEN)
    assert result.outcome == "nothing_to_reopen"
    assert await bodies(ctx["session_factory"], result.outbound_ids) == [
        "Ya se están tomando pedidos; no hay ningún cierre en vigor."
    ]
    assert await overrides(ctx["session_factory"]) == []


# --- open_window and the daily report ----------------------------------------------------------


async def test_opening_until_midnight_moves_the_report(ctx: dict[str, Any], clock: Clock) -> None:
    sf = ctx["session_factory"]
    request = await say(ctx, MIDNIGHT)
    assert await bodies(sf, request.outbound_ids) == [
        "Entendí esto:\nEl 09/10/2026 se toman pedidos también de 23:00 a 00:00 (del 10/10). "
        "Horario de ese día: 09:00 a 00:00. El reporte del día se genera a las 00:00.\n\n"
        "¿Lo aplico? Respondé *sí* o *no* (tenés 5 minutos)."
    ]
    await say(ctx, "sí")
    decision = await accepting(ctx, at(9, 23, 30))
    assert (decision.accepting, decision.business_date) == (True, D)
    assert not (await accepting(ctx, at(10, 0, 0))).accepting


async def test_opening_after_the_report_was_sent_reopens_it(
    ctx: dict[str, Any], clock: Clock, engine: Any
) -> None:
    sf = ctx["session_factory"]
    await f.insert_ok(
        engine, f.daily_report(report_date=D, status="sent", due_at=at(9, 23), current_revision=1)
    )
    clock.now = at(9, 23, 30)
    request = await say(ctx, UNTIL_ONE)
    assert await bodies(sf, request.outbound_ids) == [
        "Entendí esto:\nEl 09/10/2026 se toman pedidos también de 23:00 a 01:00 (del 10/10). "
        "Horario de ese día: 09:00 a 01:00. El reporte de ese día ya se envió: los pedidos "
        "nuevos irán en una corrección a las 01:00.\n\n"
        "¿Lo aplico? Respondé *sí* o *no* (tenés 5 minutos)."
    ]
    await say(ctx, "sí")
    async with sf() as session:
        report = await session.scalar(select(DailyReport).where(DailyReport.report_date == D))
    [opening] = [r for r in await overrides(sf) if r.kind == "open_window"]
    assert (report.status, report.due_at, report.reopened_by_override_id) == (
        "correction_pending",
        at(10, 1),
        opening.id,
    )
    assert report.correction_requested_at is not None
    assert (await accepting(ctx, at(9, 23, 45))).accepting


# --- expiry ------------------------------------------------------------------------------------


async def test_yes_after_the_deadline(ctx: dict[str, Any], clock: Clock) -> None:
    await say(ctx, RAIN)
    clock.now = at(9, 12, 5)  # exactly the deadline: expired
    result = await say(ctx, "sí")
    assert result.outcome == "confirmation_expired"
    assert await bodies(ctx["session_factory"], result.outbound_ids) == [
        "La confirmación venció y no cambié nada. Si querés, mandame la instrucción de nuevo."
    ]
    [row] = await overrides(ctx["session_factory"])
    assert row.status == "expired"
    assert (await accepting(ctx, at(9, 13))).accepting


async def test_sweep_expires_silently_then_yes_is_told(ctx: dict[str, Any], clock: Clock) -> None:
    await say(ctx, RAIN)
    clock.now = at(9, 12, 4)
    assert await schedules.expire_schedule_confirmations(ctx) == 0
    clock.now = at(9, 12, 6)
    assert await schedules.expire_schedule_confirmations(ctx) == 1
    assert await schedules.expire_schedule_confirmations(ctx) == 0
    assert len(await outbox(ctx["session_factory"])) == 1  # only the confirm request
    result = await say(ctx, "sí")
    assert result.outcome == "confirmation_expired"


# --- races and idempotency ---------------------------------------------------------------------


async def test_concurrent_yes_applies_once(ctx: dict[str, Any]) -> None:
    await say(ctx, RAIN)
    results = await asyncio.gather(*(say(ctx, "sí") for _ in range(4)))
    assert sorted(r.outcome for r in results) == ["applied"] + ["nothing_pending"] * 3
    applied = [m for m in await outbox(ctx["session_factory"]) if m.template_key == APPLIED]
    assert len(applied) == 1


async def test_concurrent_confirmations_without_the_lock(ctx: dict[str, Any]) -> None:
    """The row lock alone guarantees a single confirmation (Redis lock lost or bypassed)."""
    await say(ctx, RAIN)
    sf, settings = ctx["session_factory"], ctx["settings"]

    async def confirm() -> str:
        async with sf() as session, session.begin():
            result = await schedule_admin.confirm_pending(session, settings, admin_text("sí"))
            await asyncio.sleep(0.1)  # hold the row lock
            return result.outcome

    outcomes = await asyncio.gather(*(confirm() for _ in range(5)))
    assert sorted(outcomes) == ["applied"] + ["nothing_pending"] * 4
    assert [r.status for r in await overrides(sf)] == ["active"]


async def is_waiting_for_lock(engine: Any) -> bool:
    async with engine.connect() as conn:
        waiting = await conn.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE wait_event_type = 'Lock' AND datname = current_database()"
            )
        )
    return bool(waiting)


async def test_confirm_holding_the_row_beats_the_expiry_sweep(
    ctx: dict[str, Any], clock: Clock, engine: Any
) -> None:
    """Confirmation first: it holds the row; the sweep waits, re-checks and expires nothing."""
    sf, settings = ctx["session_factory"], ctx["settings"]
    await say(ctx, RAIN)
    clock.now = at(9, 12, 4)
    release = asyncio.Event()

    async def confirm() -> str:
        async with sf() as session, session.begin():
            result = await schedule_admin.confirm_pending(session, settings, admin_text("sí"))
            await release.wait()
            return result.outcome

    confirming = asyncio.create_task(confirm())
    await asyncio.sleep(0.1)
    clock.now = at(9, 12, 6)  # the deadline passes while the confirmation is in flight
    sweeping = asyncio.create_task(schedules.expire_schedule_confirmations(ctx))
    await asyncio.sleep(0.2)
    assert not sweeping.done() and await is_waiting_for_lock(engine)
    release.set()

    assert await confirming == "applied"
    assert await sweeping == 0
    assert [r.status for r in await overrides(sf)] == ["active"]


async def test_expiry_sweep_holding_the_row_beats_the_confirmation(
    ctx: dict[str, Any], clock: Clock, engine: Any
) -> None:
    """Sweep first: the confirmation waits, then finds no pending request and says it expired."""
    sf = ctx["session_factory"]
    await say(ctx, RAIN)
    clock.now = at(9, 12, 6)
    release = asyncio.Event()

    async def sweep() -> list[int]:
        async with sf() as session, session.begin():
            ids = await schedule_admin.expire_schedule_confirmations(session)
            await release.wait()
            return ids

    sweeping = asyncio.create_task(sweep())
    await asyncio.sleep(0.1)
    confirming = asyncio.create_task(say(ctx, "sí"))
    await asyncio.sleep(0.2)
    assert not confirming.done() and await is_waiting_for_lock(engine)
    release.set()

    assert len(await sweeping) == 1
    result = await confirming
    assert result.outcome == "confirmation_expired"
    assert [r.status for r in await overrides(sf)] == ["expired"]


async def test_replayed_confirmation_does_not_resend(ctx: dict[str, Any]) -> None:
    await say(ctx, RAIN)
    msg = admin_text("sí")
    first = await schedules.handle_admin_text(ctx, msg)
    again = await schedules.handle_admin_text(ctx, msg)  # same inbound message
    assert (first.outcome, again.outcome) == ("applied", "nothing_pending")
    applied = [m for m in await outbox(ctx["session_factory"]) if m.template_key == APPLIED]
    assert len(applied) == 1


# --- Redis admin lock: no DB connection during the LLM call ---------------------------------


async def test_slow_parser_does_not_hold_pool_connections(
    worker_ctx: Ctx, clock: Clock, database_url: str
) -> None:
    small = create_async_engine(database_url, pool_size=1, max_overflow=0, pool_timeout=1)
    try:
        ctx = worker_ctx()
        ctx["session_factory"] = async_sessionmaker(small, expire_on_commit=False)
        parser = StubParser(delay=2.0)
        ctx["schedule_parser"] = parser

        async def probe() -> int:
            await asyncio.sleep(0.7)
            checked_out = small.pool.checkedout()
            async with ctx["session_factory"]() as session:
                await session.execute(text("SELECT 1"))
            return checked_out

        started = time.monotonic()
        a, b, checked_out = await asyncio.gather(
            say(ctx, RAIN, ADMIN), say(ctx, WINDOW, OTHER_ADMIN), probe()
        )
        assert (a.outcome, b.outcome) == ("confirmation_requested", "confirmation_requested")
        assert parser.max_running == 2  # both admins parsed at once with a pool of 1
        assert checked_out == 0
        assert time.monotonic() - started < 2.0 * 2
    finally:
        await small.dispose()


async def test_one_admin_is_serialized_by_the_lock(ctx: dict[str, Any]) -> None:
    parser = StubParser(delay=0.3)
    ctx["schedule_parser"] = parser
    await asyncio.gather(say(ctx, WINDOW), say(ctx, RAIN))
    assert parser.max_running == 1
    statuses = sorted(r.status for r in await overrides(ctx["session_factory"]))
    assert statuses == ["pending_confirmation", "superseded"]
    assert await ctx["redis"].get(admin_lock_key(ADMIN)) is None


async def test_pending_changed_during_parse_retries(ctx: dict[str, Any]) -> None:
    sf = ctx["session_factory"]

    class Meddler(StubParser):
        async def parse(self, text: str, context: ScheduleContext) -> ScheduleOverrideIntent:
            result = await super().parse(text, context)
            if len(self.calls) == 1:  # another worker stores a request behind the lock's back
                async with sf() as session, session.begin():
                    await schedule_admin._apply_instruction(
                        session,
                        ctx["settings"],
                        admin_text(WINDOW),
                        schedule_admin._Snapshot(at(9, 12), None),
                        INTENTS[WINDOW],
                    )
            return result

    meddler = Meddler()
    ctx["schedule_parser"] = meddler
    result = await say(ctx, RAIN)

    assert result.outcome == "confirmation_requested"
    assert len(meddler.calls) == 2
    rows = await overrides(sf)
    assert [(r.kind, r.status) for r in rows] == [
        ("closed_window", "superseded"),
        ("closed_today", "pending_confirmation"),
    ]


async def test_pending_retries_are_bounded(ctx: dict[str, Any]) -> None:
    sf = ctx["session_factory"]

    class AlwaysMeddles(StubParser):
        async def parse(self, text: str, context: ScheduleContext) -> ScheduleOverrideIntent:
            result = await super().parse(text, context)
            async with sf() as session, session.begin():
                pending = await schedule_admin._lock_pending(session, ADMIN)
                await schedule_admin._apply_instruction(
                    session,
                    ctx["settings"],
                    admin_text(WINDOW),
                    schedule_admin._Snapshot(at(9, 12), pending.id if pending else None),
                    INTENTS[WINDOW],
                )
            return result

    ctx["schedule_parser"] = AlwaysMeddles()
    with pytest.raises(schedule_admin.PendingChangedError):
        await say(ctx, RAIN)
    assert ctx["schedule_parser"].calls and len(ctx["schedule_parser"].calls) == 3
    assert await ctx["redis"].get(admin_lock_key(ADMIN)) is None


# --- reminder of a closure until further notice (RF-45) ----------------------------------------


async def test_still_closed_reminder_once_per_day(ctx: dict[str, Any], clock: Clock) -> None:
    await say(ctx, UNTIL_NOTICE)
    await say(ctx, "sí")
    clock.now = at(9, 23)
    assert await schedules.remind_still_closed(ctx) == 0  # started today, after 09:00
    clock.now = at(10, 8, 59)
    assert await schedules.remind_still_closed(ctx) == 0  # before BUSINESS_HOURS_START
    clock.now = at(10, 9, 0)
    assert await schedules.remind_still_closed(ctx) == 2  # one per admin
    clock.now = at(10, 15, 0)
    assert await schedules.remind_still_closed(ctx) == 0  # once per day
    reminders = [m for m in await outbox(ctx["session_factory"]) if m.template_key == STILL_CLOSED]
    assert reminders[0].body == (
        "🔒 Recordatorio: la toma de pedidos sigue cerrada hasta nuevo aviso "
        "(desde el 09/10/2026). Para reabrir, escribí _ya tomamos pedidos de nuevo_."
    )
    assert {(SEND_OUTBOUND, (m.id,)) for m in reminders} <= set(
        await queued(ctx["redis"], SEND_OUTBOUND)
    )
    clock.now = at(11, 9, 0)
    assert await schedules.remind_still_closed(ctx) == 2  # the next day again


# --- business day rollover at 05:00 ------------------------------------------------------------


async def test_closed_today_confirmed_after_rollover_closes_the_new_day(
    worker_ctx: Ctx, clock: Clock
) -> None:
    """Requested at 00:58 during an extension of 09/10, confirmed at 05:02 (business day 10/10;
    a long TTL makes it possible): the closure runs from the confirmation to the end of the
    confirmation's business day, not of the request's."""
    ctx = worker_ctx(schedule_confirmation_ttl_minutes=300)
    ctx["schedule_parser"] = StubParser()
    clock.now = at(9, 22)
    await say(ctx, UNTIL_ONE)
    await say(ctx, "sí")
    clock.now = at(10, 0, 58)
    request = await say(ctx, RAIN)
    assert request.outcome == "confirmation_requested"
    pending = (await overrides(ctx["session_factory"]))[-1]
    assert (pending.business_date, pending.ends_at) == (D, at(10, 5))

    clock.now = at(10, 5, 2)
    await say(ctx, "sí")
    closure = (await overrides(ctx["session_factory"]))[-1]
    assert (closure.business_date, closure.starts_at, closure.ends_at) == (
        D + dt.timedelta(days=1),
        at(10, 5, 2),
        at(11, 5),
    )
    assert (await accepting(ctx, at(10, 12))).reason == AcceptanceReason.CLOSED
    assert (await accepting(ctx, at(11, 9))).accepting


async def test_acceptance_around_the_rollover_from_the_database(
    ctx: dict[str, Any], clock: Clock
) -> None:
    await say(ctx, RAIN)
    await say(ctx, "sí")
    before = await accepting(ctx, at(10, 4, 59))
    after = await accepting(ctx, at(10, 5, 0))
    assert (before.reason, before.business_date) == (AcceptanceReason.CLOSED, D)
    assert (after.reason, after.business_date) == (
        AcceptanceReason.OUTSIDE_HOURS,
        D + dt.timedelta(days=1),
    )


# --- precedence with persisted overrides: the most recently confirmed wins ---------------------


async def test_later_opening_reopens_part_of_closed_today(
    ctx: dict[str, Any], clock: Clock
) -> None:
    """The auditor's example: "hoy no tomamos por lluvia" confirmed at 10:00, then "abrimos de 19
    a 23" confirmed at 18:00 → open 19-23."""
    sf = ctx["session_factory"]
    clock.now = at(9, 10)
    await say(ctx, RAIN)
    await say(ctx, "sí")
    clock.now = at(9, 18)
    request = await say(ctx, EVENING)
    assert request.outcome == "confirmation_requested"
    assert await bodies(sf, request.outbound_ids) == [
        "Entendí esto:\nEl 09/10/2026 se toman pedidos también de 19:00 a 23:00. "
        "Horario de ese día: 09:00 a 10:00 y 19:00 a 23:00. "
        "Reemplaza, en el horario que se superpone: cierre de hoy por lluvia.\n\n"
        "¿Lo aplico? Respondé *sí* o *no* (tenés 5 minutos)."
    ]
    clock.now = at(9, 18, 1)
    applied = await say(ctx, "sí")
    assert await bodies(sf, applied.outbound_ids) == [
        "✅ Listo. El 09/10/2026 se toman pedidos también de 19:00 a 23:00. "
        "Horario de ese día: 09:00 a 10:00 y 19:00 a 23:00. "
        "Reemplaza, en el horario que se superpone: cierre de hoy por lluvia.\n"
        "Ahora: cerrado (lluvia)"
    ]
    assert (await accepting(ctx, at(9, 18, 30))).reason == AcceptanceReason.CLOSED
    assert (await accepting(ctx, at(9, 20))).accepting
    late = await accepting(ctx, at(9, 23, 30))
    assert (late.reason, late.closure.kind) == (AcceptanceReason.CLOSED, K.CLOSED_TODAY)
    [rain, evening] = await overrides(sf)
    assert (rain.status, evening.status) == ("active", "active")  # nothing is lifted


async def test_later_closed_today_closes_an_earlier_opening(
    ctx: dict[str, Any], clock: Clock
) -> None:
    sf = ctx["session_factory"]
    clock.now = at(9, 10)
    await say(ctx, UNTIL_ONE)
    await say(ctx, "sí")
    clock.now = at(9, 18)
    request = await say(ctx, RAIN)
    assert await bodies(sf, request.outbound_ids) == [
        "Entendí esto:\nHoy 09/10/2026 no se toman más pedidos (motivo: lluvia). "
        "Reemplaza, en el horario que se superpone: apertura del 09/10/2026 de 23:00 a "
        "01:00 (del 10/10).\n\n"
        "¿Lo aplico? Respondé *sí* o *no* (tenés 5 minutos)."
    ]
    await say(ctx, "sí")
    assert not (await accepting(ctx, at(10, 0, 30))).accepting
    async with sf() as session:
        hours = await repository.todays_hours(session, ctx["settings"], at(9, 18))
    assert effective_hours(hours, ctx["settings"]) == "09:00 a 18:00"


async def test_order_is_decided_at_confirmation_not_at_request(
    ctx: dict[str, Any], clock: Clock
) -> None:
    """ADMIN asks for an opening, OTHER_ADMIN confirms a closure meanwhile, then ADMIN says "sí":
    the opening was confirmed last, so it wins, and the applied summary says what it replaces."""
    sf = ctx["session_factory"]
    first = await say(ctx, UNTIL_ONE)
    assert "Reemplaza" not in (await bodies(sf, first.outbound_ids))[0]
    clock.now = at(9, 12, 1)
    await say(ctx, RAIN, OTHER_ADMIN)
    await say(ctx, "sí", OTHER_ADMIN)
    clock.now = at(9, 12, 2)
    applied = await say(ctx, "sí")
    [body] = await bodies(sf, applied.outbound_ids)
    assert "Reemplaza, en el horario que se superpone: cierre de hoy por lluvia." in body
    assert (await accepting(ctx, at(10, 0, 30))).accepting
    assert not (await accepting(ctx, at(9, 20))).accepting


async def test_opening_after_until_further_notice(ctx: dict[str, Any], clock: Clock) -> None:
    """A later opening overrides a closure until further notice only inside its window; the
    still-closed reminder waits while that opening is in effect."""
    clock.now = at(8, 21)
    await say(ctx, UNTIL_NOTICE)
    await say(ctx, "sí")
    clock.now = at(9, 8)
    INTENTS["abrimos de 9 a 11"] = ScheduleOverrideIntent(
        kind=K.OPEN_WINDOW, start_time=dt.time(9), end_time=dt.time(11)
    )
    try:
        assert (await say(ctx, "abrimos de 9 a 11")).outcome == "confirmation_requested"
        await say(ctx, "sí")
    finally:
        del INTENTS["abrimos de 9 a 11"]
    assert (await accepting(ctx, at(9, 10))).accepting
    assert (await accepting(ctx, at(9, 11))).closure.kind == K.CLOSED_UNTIL_FURTHER_NOTICE
    clock.now = at(9, 9)
    assert await schedules.remind_still_closed(ctx) == 0  # open right now
    clock.now = at(9, 11)
    assert await schedules.remind_still_closed(ctx) == 2  # closed again


async def test_later_closure_inside_an_opening_wins(ctx: dict[str, Any], clock: Clock) -> None:
    """Opening first, closure confirmed later: the closure wins in the overlap."""
    INTENTS["de 23:30 a 00:30 no"] = ScheduleOverrideIntent(
        kind=K.CLOSED_WINDOW, start_time=dt.time(23, 30), end_time=dt.time(0, 30)
    )
    try:
        await say(ctx, UNTIL_ONE)
        await say(ctx, "sí")
        assert (await say(ctx, "de 23:30 a 00:30 no")).outcome == "confirmation_requested"
        await say(ctx, "sí")
    finally:
        del INTENTS["de 23:30 a 00:30 no"]
    assert (await accepting(ctx, at(9, 23, 15))).accepting
    assert (await accepting(ctx, at(10, 0, 0))).reason == AcceptanceReason.CLOSED
    assert (await accepting(ctx, at(10, 0, 45))).accepting
    async with ctx["session_factory"]() as session:
        hours = await repository.todays_hours(session, ctx["settings"], at(9, 13))
    assert effective_hours(hours, ctx["settings"]) == "09:00 a 23:30 y 00:30 a 01:00"


async def test_window_inside_closed_today_is_rejected(ctx: dict[str, Any]) -> None:
    await say(ctx, RAIN)
    await say(ctx, "sí")
    result = await say(ctx, WINDOW)
    assert result.outcome == "invalid:closure_outside_hours"
    assert await bodies(ctx["session_factory"], result.outbound_ids) == [
        "No pude aplicar eso: en ese horario ya no se toman pedidos. Probá de nuevo, "
        "por ejemplo: _de 15 a 18 no tomamos pedidos_ o _hoy hasta las 00_."
    ]
