"""Daily report and corrections (CU-16, CU-17; docs/03 "Efecto sobre el reporte diario").

One version of the report is built in three steps, so no transaction is open while the file
is built:

1. :func:`step`, a short transaction holding the ``daily_reports`` row (``FOR UPDATE SKIP
   LOCKED``): recomputes ``due_at`` while ``waiting``, share-locks the orders of the day
   (``NOWAIT``; a transition in flight makes it retry on the next run), waits while there are
   pending orders (with the one-time reminder), and otherwise reads the rows, takes the snapshot
   and stores the revision as ``generating`` (``attempts + 1``). The report goes ``generating``.
2. The ``.xlsx`` is built from that in-memory data, outside any transaction.
3. :func:`store_file` stores it only if the revision is still that attempt, marks it
   ``generated`` and enqueues one ``document`` per admin (``daily_report:{D}:v{N}:{phone}``);
   :func:`fail_build` records a build error and, after ``DAILY_REPORT_MAX_ATTEMPTS``, fails the
   report and alerts (``admin_daily_report_failed``).

``send_outbound`` calls :func:`on_document_sent` / :func:`on_document_failed`: once every copy of
the revision is ``sent``, the revision is ``sent``, ``current_revision`` moves to it and a report
still ``generating`` goes ``sent``. A report that a change moved to ``correction_pending``
meanwhile stays so. Locks are always taken report row first, then revision row.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from zoneinfo import ZoneInfo

from sqlalchemy import exists, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ya_voy.config import Settings
from ya_voy.db.models import DailyReport, OutboundMessage, ReportRevision
from ya_voy.db.queries import enqueue_outbound_message
from ya_voy.domain.enums import (
    DailyReportStatus,
    OutboundMessageType,
    OutboundStatus,
    ReportRevisionKind,
    ReportRevisionStatus,
)
from ya_voy.messaging import catalog
from ya_voy.messaging import report_texts as texts
from ya_voy.messaging.outbox import enqueue_admin_alert, recipient_name
from ya_voy.reports import repository as repo
from ya_voy.reports.model import Change, ReportData, diff_snapshots, file_name, snapshot_of
from ya_voy.reports.xlsx import build_workbook
from ya_voy.schedule import repository as schedule_repository
from ya_voy.schedule.calendar import business_date
from ya_voy.whatsapp.jids import jid_for_phone

logger = logging.getLogger(__name__)

RS = DailyReportStatus
VS = ReportRevisionStatus
STALE_GENERATION = dt.timedelta(minutes=10)  # CU-16: "generating" longer than this is retried
CATCHUP_DAYS = 7  # how far back a worker that was down looks for days without a report
ERROR_LIMIT = 1000
LOCK_NOT_AVAILABLE = "55P03"

Builder = Callable[[ReportData, ZoneInfo], bytes]


class Outcome(StrEnum):
    NOT_DUE = "not_due"
    BUSY = "busy"  # row or orders locked by someone else: next run
    WAITING = "waiting"  # pending orders of the day
    IN_FLIGHT = "in_flight"  # a revision is being built or delivered
    STARTED = "started"
    NO_CHANGES = "no_changes"  # correction without net changes: back to sent (RN-23)
    SKIPPED = "skipped"  # sent / failed / no recipients


@dataclass(frozen=True)
class BuildJob:
    revision_id: int
    attempt: int
    data: ReportData


@dataclass
class StepResult:
    outcome: Outcome
    outbound_ids: list[int] = field(default_factory=list)
    job: BuildJob | None = None


async def _now(session: AsyncSession) -> dt.datetime:
    return await schedule_repository.db_now(session)


def _is_lock_not_available(exc: DBAPIError) -> bool:
    orig = exc.orig
    return LOCK_NOT_AVAILABLE in (getattr(orig, "sqlstate", None), getattr(orig, "pgcode", None))


async def _lock_report(session: AsyncSession, day: dt.date) -> DailyReport | None:
    return await session.scalar(
        select(DailyReport).where(DailyReport.report_date == day).with_for_update(skip_locked=True)
    )


async def _latest_revision(session: AsyncSession, report_id: int) -> ReportRevision | None:
    return await session.scalar(
        select(ReportRevision)
        .where(ReportRevision.daily_report_id == report_id)
        .order_by(ReportRevision.revision.desc())
        .limit(1)
    )


async def _revision(session: AsyncSession, report_id: int, number: int) -> ReportRevision:
    rev = await session.scalar(
        select(ReportRevision).where(
            ReportRevision.daily_report_id == report_id, ReportRevision.revision == number
        )
    )
    assert rev is not None
    return rev


def _in_flight(
    rev: ReportRevision | None, report: DailyReport, settings: Settings, now: dt.datetime
) -> bool:
    """A revision after the last one sent that must not be (re)started now."""
    if rev is None or rev.revision <= report.current_revision:
        return False
    if rev.status == VS.GENERATING:
        # last_checked_at is the start of the attempt: nothing else moves it while generating.
        started = report.last_checked_at
        return started is not None and started > now - STALE_GENERATION
    if rev.status == VS.FAILED:
        return rev.attempts >= settings.daily_report_max_attempts
    return True  # generated: being delivered


async def _remind_pending(
    session: AsyncSession,
    settings: Settings,
    report: DailyReport,
    codes: list[str],
    now: dt.datetime,
) -> list[int]:
    """RF-50 / CU-16 3b: once per effective close, ``REPORT_PENDING_REMINDER_MINUTES`` after
    ``due_at``. Before the first send, or for a report reopened by an extra opening (RF-55);
    not for corrections caused by releases or cancellations (CU-17)."""
    correction = report.current_revision > 0
    if (
        report.pending_reminder_sent_at is not None
        or now < report.due_at + dt.timedelta(minutes=settings.report_pending_reminder_minutes)
        or (correction and report.reopened_by_override_id is None)
        or not codes  # only drafts: they have no code and expire on their own (docs/05)
    ):
        return []
    report.pending_reminder_sent_at = now
    due_key = report.due_at.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    return await enqueue_admin_alert(
        session,
        settings,
        template_key=catalog.ADMIN_REPORT_PENDING_REMINDER,
        idempotency_base=f"{catalog.ADMIN_REPORT_PENDING_REMINDER}:{report.report_date}:{due_key}",
        variables=texts.pending_reminder_variables(
            report.report_date, codes, correction=correction
        ),
    )


async def step(
    session: AsyncSession, settings: Settings, day: dt.date, *, now: dt.datetime | None = None
) -> StepResult:
    """Advance the report of ``day`` by one step. Call inside a short transaction."""
    now = now or await _now(session)
    report = await _lock_report(session, day)
    if report is None:
        due = await repo.due_at(session, settings, day)
        if now < due:
            return StepResult(Outcome.NOT_DUE)
        await session.execute(
            insert(DailyReport)
            .values(report_date=day, status=RS.WAITING, due_at=due)
            .on_conflict_do_nothing(index_elements=[DailyReport.report_date])
        )
        report = await _lock_report(session, day)
        if report is None:
            return StepResult(Outcome.BUSY)
    if report.status in (RS.SENT, RS.FAILED):
        return StepResult(Outcome.SKIPPED)
    if report.status == RS.WAITING:
        report.due_at = await repo.due_at(session, settings, day)  # an opening may move it
    if now < report.due_at:
        return StepResult(Outcome.NOT_DUE)

    latest = await _latest_revision(session, report.id)
    if _in_flight(latest, report, settings, now):
        return StepResult(Outcome.IN_FLIGHT)
    unsent = latest if latest is not None and latest.revision > report.current_revision else None

    try:
        async with session.begin_nested():
            orders = await repo.lock_day_orders(session, day)
    except DBAPIError as exc:
        if not _is_lock_not_available(exc):
            raise
        return StepResult(Outcome.BUSY)
    count, codes = repo.pending_codes(orders)
    report.pending_orders_count = count
    report.last_checked_at = now
    if count:
        return StepResult(
            Outcome.WAITING, await _remind_pending(session, settings, report, codes, now)
        )
    if not settings.admin_phones:
        logger.warning("daily report skipped: no ADMIN_PHONES", extra={"report_date": str(day)})
        return StepResult(Outcome.SKIPPED)

    rows = await repo.load_order_rows(session, day)
    snapshot = snapshot_of(rows)
    changes: list[Change] = []
    number = unsent.revision if unsent is not None else report.current_revision + 1
    if number > 1:
        previous = await _revision(session, report.id, report.current_revision)
        changes = diff_snapshots(previous.snapshot, snapshot)
        if not changes:  # CU-17 4a / PA-01: nothing to correct
            if unsent is not None:
                unsent.status = VS.FAILED
                unsent.last_error = "superseded: no net changes"
            report.status = RS.SENT
            report.reopened_by_override_id = None
            report.correction_requested_at = None
            return StepResult(Outcome.NO_CHANGES)

    data = ReportData(
        report_date=day,
        revision=number,
        orders=rows,
        couriers=await repo.load_courier_rows(session, day),
        changes=changes,
        schedule_changes=(
            await repo.schedule_change_items(session, settings, day) if number == 1 else []
        ),
    )
    if unsent is None:
        unsent = ReportRevision(
            daily_report_id=report.id,
            revision=number,
            kind=ReportRevisionKind.INITIAL if number == 1 else ReportRevisionKind.CORRECTION,
            attempts=0,
        )
        session.add(unsent)
    unsent.status = VS.GENERATING
    unsent.snapshot = snapshot
    unsent.changes = [c.as_json() for c in changes]
    unsent.attempts += 1
    unsent.file_content = None
    unsent.last_error = None
    report.status = RS.GENERATING
    report.correction_requested_at = None  # this version covers every change up to now
    await session.flush()
    return StepResult(Outcome.STARTED, job=BuildJob(unsent.id, unsent.attempts, data))


async def _lock_revision_and_report(
    session: AsyncSession, revision_id: int
) -> tuple[DailyReport, ReportRevision]:
    report_id = await session.scalar(
        select(ReportRevision.daily_report_id).where(ReportRevision.id == revision_id)
    )
    report = await session.scalar(
        select(DailyReport).where(DailyReport.id == report_id).with_for_update()
    )
    rev = await session.scalar(
        select(ReportRevision).where(ReportRevision.id == revision_id).with_for_update()
    )
    assert report is not None and rev is not None
    return report, rev


async def store_file(
    session: AsyncSession, settings: Settings, job: BuildJob, content: bytes
) -> list[int]:
    """Step 3: keep the file and enqueue the documents, unless another attempt took over."""
    report, rev = await _lock_revision_and_report(session, job.revision_id)
    if rev.status != VS.GENERATING or rev.attempts != job.attempt:
        logger.info("report attempt superseded", extra={"report_revision_id": rev.id})
        return []
    rev.file_name = file_name(report.report_date, rev.revision)
    rev.file_content = content
    rev.status = VS.GENERATED
    rev.generated_at = await _now(session)
    template_key, body = texts.caption(job.data)
    ids: list[int] = []
    for phone in settings.admin_phones:
        new_id = await enqueue_outbound_message(
            session,
            idempotency_key=f"daily_report:{report.report_date}:v{rev.revision}:{phone}",
            template_key=template_key,
            recipient_jid=jid_for_phone(phone),
            message_type=OutboundMessageType.DOCUMENT,
            body=body,
            report_revision_id=rev.id,
        )
        if new_id is not None:
            ids.append(new_id)
    return ids


async def _fail_report(
    session: AsyncSession, settings: Settings, report: DailyReport, revision: int, error: str
) -> list[int]:
    report.status = RS.FAILED
    return await enqueue_admin_alert(
        session,
        settings,
        template_key=catalog.ADMIN_DAILY_REPORT_FAILED,
        idempotency_base=f"{catalog.ADMIN_DAILY_REPORT_FAILED}:{report.report_date}:v{revision}",
        variables=texts.report_failed_variables(report.report_date, revision, error),
    )


async def fail_build(
    session: AsyncSession, settings: Settings, job: BuildJob, exc: BaseException
) -> list[int]:
    """The file could not be built: retried on the next run, up to the maximum."""
    report, rev = await _lock_revision_and_report(session, job.revision_id)
    if rev.status != VS.GENERATING or rev.attempts != job.attempt:
        return []
    rev.status = VS.FAILED
    rev.last_error = f"{type(exc).__name__}: {exc}"[:ERROR_LIMIT]  # stored, never sent
    logger.error(
        "daily report build failed",
        extra={"report_revision_id": rev.id, "attempts": rev.attempts},
    )
    if rev.attempts < settings.daily_report_max_attempts:
        return []
    return await _fail_report(
        session, settings, report, rev.revision, catalog.REPORT_ERROR_GENERATION
    )


async def on_document_sent(session: AsyncSession, row: OutboundMessage) -> None:
    """In ``send_outbound``'s transaction, after the row became ``sent``."""
    assert row.report_revision_id is not None
    await session.flush()
    report, rev = await _lock_revision_and_report(session, row.report_revision_id)
    if rev.status != VS.GENERATED:
        return
    unsent = await session.scalar(
        select(
            exists().where(
                OutboundMessage.report_revision_id == rev.id,
                OutboundMessage.status != OutboundStatus.SENT,
            )
        )
    )
    if unsent:
        return
    rev.status = VS.SENT
    rev.sent_at = await _now(session)
    report.current_revision = max(report.current_revision, rev.revision)
    if report.status == RS.GENERATING:
        report.status = RS.SENT
        report.reopened_by_override_id = None
        report.correction_requested_at = None


async def on_document_failed(
    session: AsyncSession, settings: Settings, row: OutboundMessage
) -> list[int]:
    """A copy could not be delivered: the revision and the report fail and the admins get
    ``admin_daily_report_failed`` (instead of the generic ``admin_outbound_failed``)."""
    assert row.report_revision_id is not None
    report, rev = await _lock_revision_and_report(session, row.report_revision_id)
    if rev.status == VS.GENERATED:
        rev.status = VS.FAILED
        rev.last_error = row.last_error
    error = catalog.REPORT_ERROR_DELIVERY.format(
        recipient_name=await recipient_name(session, settings, row.recipient_jid),
        attempts_text=catalog.attempts_text(row.attempts),
    )
    return await _fail_report(session, settings, report, rev.revision, error)


# TODO(stage-8b): wire T13/T16/T17/T18 (release and cancel flows) to mark_report_for_correction.
async def mark_report_for_correction(session: AsyncSession, day: dt.date) -> bool:
    """Docs/03 "Efecto sobre el reporte diario": call in the same transaction as T13, T16, T17
    and T18, **after** updating the order row (lock order: order, then report).

    Also matches ``generating``: a change committed after the snapshot of a version in flight
    must be corrected too.
    """
    result = await session.execute(
        text(
            """
            UPDATE daily_reports
               SET status = 'correction_pending',
                   correction_requested_at = COALESCE(correction_requested_at, now()),
                   updated_at = now()
             WHERE report_date = :day
               AND status IN ('sent', 'generating')
            """
        ),
        {"day": day},
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


# TODO(stage-8b): admin commands on reports need admin identity by number/@lid: report status
# in `estado` and a manual retry of a `failed` report.
async def report_dates(
    session: AsyncSession, settings: Settings, now: dt.datetime
) -> list[dt.date]:
    """Days ``process_daily_reports`` looks at: from the day after the last report (on a fresh
    database, the first day with orders) to today, at most ``CATCHUP_DAYS`` back, plus any report
    still waiting or generating."""
    today = business_date(now, settings.zoneinfo, settings.business_day_rollover_time)
    last = await session.scalar(select(func.max(DailyReport.report_date)))
    if last is None:  # fresh database: from the first day with orders, if any
        first = min(await repo.first_order_date(session) or today, today)
    else:
        first = last + dt.timedelta(days=1)
    first = max(first, today - dt.timedelta(days=CATCHUP_DAYS))
    days = {first + dt.timedelta(days=i) for i in range((today - first).days + 1)}
    open_rows = await session.scalars(
        select(DailyReport.report_date).where(DailyReport.status.in_([RS.WAITING, RS.GENERATING]))
    )
    return sorted(days | set(open_rows))


async def correction_dates(session: AsyncSession, now: dt.datetime) -> list[dt.date]:
    rows = await session.scalars(
        select(DailyReport.report_date)
        .where(DailyReport.status == RS.CORRECTION_PENDING, DailyReport.due_at <= now)
        .order_by(DailyReport.report_date)
    )
    return list(rows)


async def run_step(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    day: dt.date,
    *,
    builder: Builder = build_workbook,
) -> tuple[Outcome, list[int]]:
    """One step for ``day``, building the file between two short transactions. Returns the
    outcome and the outbox ids to enqueue after the commits."""
    async with session_factory() as session, session.begin():
        result = await step(session, settings, day)
    ids = list(result.outbound_ids)
    job = result.job
    if job is None:
        return result.outcome, ids
    try:
        content = await asyncio.to_thread(builder, job.data, settings.zoneinfo)
    except Exception as exc:
        async with session_factory() as session, session.begin():
            ids += await fail_build(session, settings, job, exc)
        return Outcome.STARTED, ids
    async with session_factory() as session, session.begin():
        ids += await store_file(session, settings, job, content)
    return Outcome.STARTED, ids
