"""Admin schedule changes: interpret, confirm, apply, expire (CU-14, RF-41..RF-46, RF-57, RN-24).

Everything here starts from an admin that is already recognized (``AdminText``). Recognizing the
sender as an admin by number or ``@lid`` is stage 7b; see ``TODO(stage-7b)`` in
``ya_voy/worker/inbound.py``.

Concurrency, same pattern as order intake (docs/06):

- One admin's messages are serialized by a Redis lock (``lock:admin:{phone}``) whose TTL exceeds
  the worst-case LLM time. The parser runs with no database connection checked out, between a short
  snapshot transaction and a short apply transaction.
- The apply transaction locks the admin's pending request (``SELECT ... FOR UPDATE``) and checks it
  is the one the snapshot saw (revision = its id, or none); otherwise it re-reads and retries
  (bounded). The partial unique index ``schedule_overrides_one_pending_per_admin`` backs it.
- Confirmation, rejection and the expiry sweep are conditional transitions on the
  ``pending_confirmation`` row under its row lock: exactly one of "confirm" / "expire" wins.
- Every reply goes through the outbox with a deterministic ``idempotency_key``.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from redis.asyncio import Redis
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ya_voy.config import Settings
from ya_voy.db.models import ScheduleOverride
from ya_voy.db.queries import enqueue_outbound_message
from ya_voy.domain.commands import AdminCommand, parse_admin_command
from ya_voy.domain.enums import ScheduleOverrideKind, ScheduleOverrideStatus
from ya_voy.llm.extractor import LLMError, ScheduleContext, ScheduleIntentParser
from ya_voy.llm.schemas import ScheduleOverrideIntent
from ya_voy.locks import admin_lock
from ya_voy.messaging import catalog
from ya_voy.messaging import schedule_texts as texts
from ya_voy.messaging.outbox import enqueue_admin_alert
from ya_voy.schedule.calendar import business_date
from ya_voy.schedule.hours import (
    OverrideView,
    at_local,
    closures_in_effect,
    day_bounds,
    decide_acceptance,
    effective_intervals,
    overridden_in_overlap,
    report_due_at,
    winning_closures,
)
from ya_voy.schedule.intents import (
    NotAScheduleInstructionError,
    NothingToReopenError,
    Proposal,
    ScheduleIntentInvalidError,
    upcoming_closures,
    validate_schedule_intent,
)
from ya_voy.schedule.repository import db_now, load_overrides, report_status

logger = logging.getLogger(__name__)

K = ScheduleOverrideKind
St = ScheduleOverrideStatus
MAX_APPLY_ATTEMPTS = 3
Finalize = Callable[[AsyncSession], Awaitable[None]]


@dataclass(frozen=True)
class AdminText:
    """A private text from a sender already recognized as an admin (stage 7b recognizes it)."""

    admin_phone: str  # ADMIN_PHONES entry; schedule_overrides.created_by
    reply_jid: str  # chat to answer in
    text: str
    inbound_event_id: int | None = None
    wa_message_id: str | None = None

    @property
    def message_key(self) -> str:
        if self.inbound_event_id is not None:
            return f"inbound:{self.inbound_event_id}"
        if self.wa_message_id:
            return f"msg:{self.wa_message_id}"
        raise ValueError("AdminText needs inbound_event_id or wa_message_id (outbox keys)")


@dataclass
class AdminResult:
    outcome: str
    override_id: int | None = None
    outbound_ids: list[int] = field(default_factory=list)


class PendingChangedError(Exception):
    """The admin's pending request kept changing under the parser call (lock lost repeatedly)."""


class _RetryError(Exception):
    """Raised inside the apply transaction so it rolls back, then retried from a new snapshot."""


# --- helpers -----------------------------------------------------------------------------------


async def _reply(
    session: AsyncSession, msg: AdminText, template_key: str, key: str | None = None, **variables
) -> list[int]:
    new_id = await enqueue_outbound_message(
        session,
        idempotency_key=key or f"admin_msg:{msg.message_key}:{template_key}",
        template_key=template_key,
        recipient_jid=msg.reply_jid,
        body=catalog.render(template_key, **variables),
    )
    return [new_id] if new_id is not None else []


async def _lock_pending(session: AsyncSession, admin_phone: str) -> ScheduleOverride | None:
    return await session.scalar(
        select(ScheduleOverride)
        .where(
            ScheduleOverride.created_by == admin_phone,
            ScheduleOverride.status == St.PENDING_CONFIRMATION,
        )
        .with_for_update()
    )


def _today(settings: Settings, now: dt.datetime) -> dt.date:
    return business_date(now, settings.zoneinfo, settings.business_day_rollover_time)


async def _open_window_texts(
    session: AsyncSession,
    settings: Settings,
    overrides: list[OverrideView],
    window: OverrideView,
) -> tuple[str, str]:
    """``{effective_hours}`` (with the opening) and ``{report_note}`` of an open_window summary."""
    assert window.business_date is not None and window.ends_at is not None
    day = window.business_date
    hours = texts.effective_hours(
        effective_intervals(settings, [*overrides, window], day), settings
    )
    note = texts.report_note(
        await report_status(session, day),
        window_end=window.ends_at,
        current_due_at=report_due_at(settings, overrides, day),
        settings=settings,
    )
    return hours, note


async def _summary(
    session: AsyncSession,
    settings: Settings,
    overrides: list[OverrideView],
    view: OverrideView,
    *,
    to_lift: list[OverrideView] | None = None,
    upcoming: list[OverrideView] | None = None,
    replaced: list[OverrideView] | None = None,
) -> str:
    hours = note = ""
    if view.kind == K.OPEN_WINDOW:
        hours, note = await _open_window_texts(session, settings, overrides, view)
    return texts.override_summary(
        view,
        settings,
        effective_hours_text=hours,
        report_note_text=note,
        to_lift=to_lift or [],
        upcoming=upcoming or [],
        replaced=replaced or [],
    )


def _context(settings: Settings, now: dt.datetime) -> ScheduleContext:
    return ScheduleContext(
        now_local=now.astimezone(settings.zoneinfo),
        business_date=_today(settings, now),
        hours_start=settings.business_hours_start,
        hours_end=settings.business_hours_end,
    )


# --- new instruction (CU-14 steps 1-5) ---------------------------------------------------------


@dataclass(frozen=True)
class _Snapshot:
    now: dt.datetime
    pending_id: int | None


async def _snapshot(
    session_factory: async_sessionmaker[AsyncSession], admin_phone: str
) -> _Snapshot:
    async with session_factory() as session, session.begin():
        now = await db_now(session)
        pending = await _lock_pending(session, admin_phone)
        return _Snapshot(now, pending.id if pending else None)


async def _apply_instruction(
    session: AsyncSession,
    settings: Settings,
    msg: AdminText,
    snapshot: _Snapshot,
    intent: ScheduleOverrideIntent,
) -> AdminResult:
    """Validate with fresh data and store the request; :class:`_RetryError` if the pending request
    changed since the snapshot (the transaction then rolls back, supersede included)."""
    pending = await _lock_pending(session, msg.admin_phone)
    if (pending.id if pending else None) != snapshot.pending_id:
        raise _RetryError
    now = await db_now(session)
    overrides = await load_overrides(session, settings, now)
    try:
        proposal = validate_schedule_intent(intent, settings=settings, now=now, overrides=overrides)
    except NotAScheduleInstructionError:
        return AdminResult("not_schedule", None, await _reply(session, msg, catalog.ADMIN_HELP))
    except ScheduleIntentInvalidError as exc:
        reason = texts.invalid_reason(exc.code, settings)
        ids = await _reply(session, msg, catalog.ADMIN_SCHEDULE_INVALID, reason=reason)
        return AdminResult(f"invalid:{exc.code}", None, ids)
    except NothingToReopenError as exc:
        note = texts.future_closures_note(exc.upcoming, settings)
        ids = await _reply(
            session, msg, catalog.ADMIN_SCHEDULE_NOTHING_TO_REOPEN, future_closures_note=note
        )
        return AdminResult("nothing_to_reopen", None, ids)

    # CU-14 step 3: a new valid instruction replaces the pending one (RN-24, 6c).
    if pending is not None:
        pending.status = St.SUPERSEDED
        await session.flush()
    row = _new_row(settings, msg, intent, proposal, now)
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        # schedule_overrides_one_pending_per_admin: someone else stored one meanwhile.
        raise _RetryError from None
    summary = await _summary(
        session,
        settings,
        overrides,
        proposal.as_view(),
        to_lift=proposal.to_lift,
        upcoming=proposal.upcoming,
        replaced=proposal.replaced,
    )
    ids = await _reply(
        session,
        msg,
        catalog.ADMIN_SCHEDULE_CONFIRM,
        key=f"schedule_override:{row.id}:confirm",
        override_summary=summary,
        minutes=settings.schedule_confirmation_ttl_minutes,
    )
    return AdminResult("confirmation_requested", row.id, ids)


def _new_row(
    settings: Settings,
    msg: AdminText,
    intent: ScheduleOverrideIntent,
    proposal: Proposal,
    now: dt.datetime,
) -> ScheduleOverride:
    return ScheduleOverride(
        kind=proposal.kind,
        status=St.PENDING_CONFIRMATION,
        business_date=proposal.business_date,
        starts_at=proposal.starts_at,
        ends_at=proposal.ends_at,
        reason=proposal.reason,
        source_text=msg.text,
        parsed=intent.model_dump(mode="json"),
        created_by=msg.admin_phone,
        inbound_event_id=msg.inbound_event_id,
        confirmation_expires_at=now
        + dt.timedelta(minutes=settings.schedule_confirmation_ttl_minutes),
    )


# --- confirmation (CU-14 steps 6-8) ------------------------------------------------------------


async def confirm_pending(session: AsyncSession, settings: Settings, msg: AdminText) -> AdminResult:
    """The admin answered "sí". Call inside a transaction; no LLM involved."""
    now = await db_now(session)
    row = await _lock_pending(session, msg.admin_phone)
    if row is None:
        latest = await session.scalar(
            select(ScheduleOverride)
            .where(ScheduleOverride.created_by == msg.admin_phone)
            .order_by(ScheduleOverride.id.desc())
            .limit(1)
        )
        if latest is not None and latest.status == St.EXPIRED:
            ids = await _reply(session, msg, catalog.ADMIN_SCHEDULE_CONFIRMATION_EXPIRED)
            return AdminResult("confirmation_expired", latest.id, ids)
        return AdminResult("nothing_pending", None, await _reply(session, msg, catalog.ADMIN_HELP))
    if row.confirmation_expires_at <= now:
        row.status = St.EXPIRED  # CU-14 6b: the sweep had not run yet
        ids = await _reply(session, msg, catalog.ADMIN_SCHEDULE_CONFIRMATION_EXPIRED)
        return AdminResult("confirmation_expired", row.id, ids)

    overrides_before = await load_overrides(session, settings, now)
    kind = K(row.kind)
    lifted: list[OverrideView] = []
    report_note_before = ""
    if kind == K.CLOSED_TODAY:
        # From the confirmation to the end of the business day of the confirmation (RN-26).
        today = _today(settings, now)
        row.business_date, row.starts_at, row.ends_at = today, now, day_bounds(settings, today).end
    elif kind == K.CLOSED_UNTIL_FURTHER_NOTICE:
        row.starts_at = now
    elif kind == K.OPEN_WINDOW:
        _, report_note_before = await _open_window_texts(
            session, settings, overrides_before, OverrideView.of(row)
        )
    row.status = St.APPLIED if kind == K.REOPEN else St.ACTIVE
    row.confirmed_at = now
    await session.flush()

    if kind == K.REOPEN:
        lifted = await _lift_closures(session, now, row.id, overrides_before)
    overrides_after = await load_overrides(session, settings, now)
    if kind == K.OPEN_WINDOW:
        await _reopen_daily_report(session, settings, row, overrides_after)

    view = OverrideView.of(row)
    # Recomputed at confirmation: what this instruction overrides now (most recent wins).
    replaced = overridden_in_overlap(overrides_before, view, now)
    if kind == K.OPEN_WINDOW:
        assert view.business_date is not None
        hours = texts.effective_hours(
            effective_intervals(settings, overrides_after, view.business_date), settings
        )
        summary = texts.override_summary(
            view,
            settings,
            effective_hours_text=hours,
            report_note_text=report_note_before,
            replaced=replaced,
        )
    else:
        summary = texts.override_summary(
            view,
            settings,
            to_lift=lifted,
            upcoming=upcoming_closures(settings, overrides_after, now),
            replaced=replaced,
        )
    status = texts.status_line(decide_acceptance(settings, overrides_after, now))
    ids = await _reply(
        session,
        msg,
        catalog.ADMIN_SCHEDULE_APPLIED,
        key=f"schedule_override:{row.id}:applied",
        override_summary=summary,
        status_line=status,
    )
    return AdminResult("applied", row.id, ids)


async def _lift_closures(
    session: AsyncSession, now: dt.datetime, reopen_id: int, overrides: list[OverrideView]
) -> list[OverrideView]:
    """RF-44: lift the closures in effect at confirmation; future ones and openings stay."""
    in_effect = closures_in_effect(overrides, now)
    if not in_effect:
        return []
    ids = [o.id for o in in_effect]
    lifted = (
        await session.scalars(
            update(ScheduleOverride)
            .where(ScheduleOverride.id.in_(ids), ScheduleOverride.status == St.ACTIVE)
            .values(status=St.CANCELLED, cancelled_at=now, cancelled_by_override_id=reopen_id)
            .returning(ScheduleOverride.id)
        )
    ).all()
    return [o for o in in_effect if o.id in set(lifted)]


async def _reopen_daily_report(
    session: AsyncSession,
    settings: Settings,
    opening: ScheduleOverride,
    overrides: list[OverrideView],
) -> None:
    """docs/03 "Efecto sobre el reporte diario": an opening on a reported day reopens it (RF-55).

    Waits for the report row lock if the report is being generated (CU-14 7b).
    """
    assert opening.business_date is not None
    due = report_due_at(settings, overrides, opening.business_date)
    await session.execute(
        text(
            """
            UPDATE daily_reports
               SET status = 'correction_pending',
                   due_at = :due_at,
                   reopened_by_override_id = :override_id,
                   pending_reminder_sent_at = NULL,
                   correction_requested_at = COALESCE(correction_requested_at, now()),
                   updated_at = now()
             WHERE report_date = :report_date
               AND status IN ('sent', 'generating', 'correction_pending')
            """
        ),
        {"due_at": due, "override_id": opening.id, "report_date": opening.business_date},
    )


async def reject_pending(session: AsyncSession, msg: AdminText) -> AdminResult:
    """The admin answered "no" (CU-14 6a). Without a pending request nothing changes either."""
    row = await _lock_pending(session, msg.admin_phone)
    if row is not None:
        row.status = St.REJECTED
    ids = await _reply(session, msg, catalog.ADMIN_SCHEDULE_REJECTED)
    return AdminResult("rejected" if row else "nothing_pending", row.id if row else None, ids)


# --- entry point -------------------------------------------------------------------------------


async def handle_admin_text(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    settings: Settings,
    parser: ScheduleIntentParser,
    msg: AdminText,
    *,
    finalize: Finalize | None = None,
) -> AdminResult:
    """One private text from a recognized admin. Commits on its own; the caller then enqueues
    ``send_outbound`` for ``outbound_ids``. ``finalize`` runs in the transaction that records the
    outcome (stage 7b marks the inbound event processed there)."""
    async with admin_lock(redis, settings, msg.admin_phone) as lock:
        parsed = parse_admin_command(msg.text)
        if parsed is not None:
            async with session_factory() as session, session.begin():
                if parsed.command == AdminCommand.CONFIRM:
                    result = await confirm_pending(session, settings, msg)
                elif parsed.command == AdminCommand.REJECT:
                    result = await reject_pending(session, msg)
                elif parsed.command == AdminCommand.HELP:
                    result = AdminResult(
                        "help", None, await _reply(session, msg, catalog.ADMIN_HELP)
                    )
                else:
                    # TODO(stage-7b): estado/horario (CU-15, admin_schedule_status); cancelar and
                    # liberar {código} (CU-13) -> orders.cancellation.cancel_by_admin and
                    # orders.claims.release_by_admin (stage 5a/6a core) in this transaction.
                    result = AdminResult(f"command:{parsed.command}")
                if finalize is not None:
                    await finalize(session)
            return result

        for attempt in range(1, MAX_APPLY_ATTEMPTS + 1):
            snapshot = await _snapshot(session_factory, msg.admin_phone)
            await lock.refresh()
            try:
                # No database connection is held during the LLM call.
                intent = await parser.parse(msg.text, _context(settings, snapshot.now))
            except LLMError:
                logger.warning("schedule parse failed", exc_info=True)
                async with session_factory() as session, session.begin():
                    ids = await _reply(session, msg, catalog.ADMIN_SCHEDULE_PARSE_ERROR)
                    if finalize is not None:
                        await finalize(session)
                return AdminResult("parse_error", None, ids)
            try:
                async with session_factory() as session, session.begin():
                    result = await _apply_instruction(session, settings, msg, snapshot, intent)
                    if finalize is not None:
                        await finalize(session)
                return result
            except _RetryError:
                pass
            logger.warning("pending schedule request changed, retrying", extra={"attempt": attempt})
    raise PendingChangedError(f"admin request kept changing {MAX_APPLY_ATTEMPTS} times")


# --- sweeps ------------------------------------------------------------------------------------


async def expire_schedule_confirmations(session: AsyncSession) -> list[int]:
    """CU-14 6d: pending requests past ``confirmation_expires_at`` become ``expired``, silently.

    Conditional on ``pending_confirmation``: a confirmation holding the row lock wins (the sweep
    waits and re-checks), and an expired row is never confirmed (the confirmation re-checks the
    deadline under the same lock).
    """
    now = await db_now(session)
    return list(
        (
            await session.scalars(
                update(ScheduleOverride)
                .where(
                    ScheduleOverride.status == St.PENDING_CONFIRMATION,
                    ScheduleOverride.confirmation_expires_at <= now,
                )
                .values(status=St.EXPIRED)
                .returning(ScheduleOverride.id)
            )
        ).all()
    )


async def remind_still_closed(session: AsyncSession, settings: Settings) -> list[int]:
    """RF-45 / CU-14 8a: once per business day, from ``BUSINESS_HOURS_START`` on, while a
    ``closed_until_further_notice`` that started before today's base start closes the store (no
    later opening in effect overrides it at that moment)."""
    now = await db_now(session)
    today = _today(settings, now)
    base_start = at_local(settings, today, settings.business_hours_start)
    if now < base_start:
        return []
    ids: list[int] = []
    overrides = await load_overrides(session, settings, now)
    # Only while it actually closes the store: a later opening in effect now overrides it.
    for closure in winning_closures(overrides, now):
        if not (
            closure.kind == K.CLOSED_UNTIL_FURTHER_NOTICE
            and closure.starts_at is not None
            and closure.starts_at < base_start
        ):
            continue
        since = closure.starts_at.astimezone(settings.zoneinfo).date()
        ids += await enqueue_admin_alert(
            session,
            settings,
            template_key=catalog.ADMIN_SCHEDULE_STILL_CLOSED,
            idempotency_base=f"{catalog.ADMIN_SCHEDULE_STILL_CLOSED}:{today}:{closure.id}",
            variables={
                "reason_text": texts.reason_text(closure.reason),
                "since": texts.fmt_date(since),
            },
        )
    return ids
