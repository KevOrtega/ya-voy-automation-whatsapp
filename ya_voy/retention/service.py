"""Anonymization of data older than ``DATA_RETENTION_DAYS`` (RNF-10, DC-11, RNF-09).

Rows are **never deleted**: personal fields (addresses, notes, free text, message bodies, raw
payloads, report files) are cleared and ``anonymized_at`` is set; ids, statuses, timestamps,
foreign keys and counts stay, so old reports still add up. Each category runs in batches of
``DATA_RETENTION_BATCH_SIZE`` rows, one short transaction per batch, taken with
``FOR UPDATE SKIP LOCKED`` (a row a live flow is holding is left for the next run, and two
concurrent runs split the work instead of waiting on each other). ``anonymized_at IS NULL`` makes
every run skip what an earlier one did.

Live data is never touched: orders still pending or of a day whose report can still change,
messages and events of those orders, unsent outbox rows, unfinished inbound events, schedule
overrides that are pending or still in force, and report files whose report is not ``sent`` /
``failed`` or that a queued document still needs.

Runtime payload captures (``CAPTURE_DIR``, development only) older than the window are deleted from
disk after the database work; missing files are fine. Committed fixtures
(``tests/fixtures/evolution``) are already anonymized by ``scripts/anonymize_captures.py``.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ya_voy.config import Settings
from ya_voy.db.models import sql_in
from ya_voy.domain.enums import PENDING_STATUSES
from ya_voy.schedule import repository as schedule_repository
from ya_voy.schedule.calendar import business_date

logger = logging.getLogger(__name__)

# Order statuses that hold back the daily report (docs/03): never anonymized, nor their data.
_PENDING_ORDER = "('draft', 'awaiting_confirmation', 'published')"
_ORDER_IS_PENDING = (
    f"EXISTS (SELECT 1 FROM orders o WHERE o.id = t.order_id AND o.status IN {_PENDING_ORDER})"
)


@dataclass(frozen=True)
class Category:
    name: str
    table: str
    where: str  # rows to anonymize (besides ``anonymized_at IS NULL``); alias ``t``
    set: str  # personal fields cleared; ``anonymized_at`` is added


CATEGORIES: tuple[Category, ...] = (
    Category(
        "orders",
        "orders",
        f"""t.business_date < :cutoff_date
            AND NOT {sql_in("t.status", PENDING_STATUSES)}
            AND NOT EXISTS (
                SELECT 1 FROM daily_reports r
                 WHERE r.report_date = t.business_date AND r.status NOT IN ('sent', 'failed'))""",
        """pickup_address = NULL, delivery_address = NULL, product = NULL,
           payment_method_raw = NULL, notes = NULL, extraction = '{}'::jsonb,
           validation_errors = '[]'::jsonb""",
    ),
    Category(
        "order_events",
        "order_events",
        f"t.created_at < :cutoff_ts AND NOT {_ORDER_IS_PENDING}",
        # The extraction (addresses), the admin's number and error texts; the rest is ids/emoji.
        "data = t.data - 'extraction' - 'admin_jid' - 'error'",
    ),
    Category(
        "inbound_events",
        "inbound_events",
        "t.received_at < :cutoff_ts AND t.status IN ('processed', 'ignored', 'failed')",
        "payload = '{}'::jsonb, error = NULL",
    ),
    Category(
        "messages",
        "messages",
        f"t.created_at < :cutoff_ts AND (t.order_id IS NULL OR NOT {_ORDER_IS_PENDING})",
        "body = NULL",
    ),
    Category(
        "outbound_messages",
        "outbound_messages",
        "t.created_at < :cutoff_ts AND t.status IN ('sent', 'failed')",
        "body = '', last_error = NULL",
    ),
    Category(
        "schedule_overrides",
        "schedule_overrides",
        """t.created_at < :cutoff_ts
            AND t.status <> 'pending_confirmation'
            AND NOT (t.status = 'active'
                     AND (t.ends_at IS NULL OR t.ends_at >= :cutoff_ts)
                     AND (t.business_date IS NULL OR t.business_date >= :cutoff_date))""",
        "source_text = '', parsed = '{}'::jsonb, reason = NULL",
    ),
    Category(
        "report_files",
        "report_revisions",
        """EXISTS (
                SELECT 1 FROM daily_reports r
                 WHERE r.id = t.daily_report_id
                   AND r.report_date < :cutoff_date
                   AND r.status IN ('sent', 'failed'))
            AND NOT EXISTS (
                SELECT 1 FROM outbound_messages o
                 WHERE o.report_revision_id = t.id AND o.status IN ('pending', 'sending'))""",
        # The .xlsx lists addresses. ``file_name`` is only "reporte-ya-voy-<date>.xlsx".
        "file_content = NULL",
    ),
)

# ``ya_voy.whatsapp.capture.capture_file_name``: ``20261009T031500123456Z_<event>_<hash>.json``.
_CAPTURE_NAME = re.compile(r"^(\d{8}T\d{12})Z_.+\.json$")


@dataclass(frozen=True)
class Cutoff:
    now: dt.datetime
    ts: dt.datetime  # rows created before this instant
    date: dt.date  # business days before this date


async def cutoff(session: AsyncSession, settings: Settings) -> Cutoff:
    now = await schedule_repository.db_now(session)
    days = dt.timedelta(days=settings.data_retention_days)
    today = business_date(now, settings.zoneinfo, settings.business_day_rollover_time)
    return Cutoff(now, now - days, today - days)


def _batch_sql(category: Category) -> str:
    return f"""
        WITH batch AS (
            SELECT t.id FROM {category.table} t
             WHERE t.anonymized_at IS NULL AND {category.where}
             ORDER BY t.id
             LIMIT :batch
               FOR UPDATE SKIP LOCKED
        )
        UPDATE {category.table} t
           SET {category.set}, anonymized_at = :now
          FROM batch
         WHERE t.id = batch.id
        RETURNING t.id
    """


async def anonymize_batch(
    session: AsyncSession, category: Category, limits: Cutoff, batch_size: int
) -> int:
    rows = await session.execute(
        text(_batch_sql(category)),
        {
            "now": limits.now,
            "cutoff_ts": limits.ts,
            "cutoff_date": limits.date,
            "batch": batch_size,
        },
    )
    return len(rows.all())


async def anonymize_category(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    category: Category,
    limits: Cutoff,
) -> int:
    """Batches until one comes back short (done, or the rest is locked by someone else)."""
    total = 0
    batch_size = settings.data_retention_batch_size
    while True:
        async with session_factory() as session, session.begin():
            done = await anonymize_batch(session, category, limits, batch_size)
        total += done
        if done < batch_size:
            return total


def delete_old_captures(directory: str | Path, before: dt.datetime) -> int:
    """Development payload captures older than ``before`` (by the time in their name)."""
    folder = Path(directory)
    if not folder.is_dir():
        return 0
    deleted = 0
    for path in folder.iterdir():
        match = _CAPTURE_NAME.match(path.name)
        if match is None:
            continue  # not a capture (e.g. the pseudonym mapping): left to the operator
        taken = dt.datetime.strptime(match.group(1), "%Y%m%dT%H%M%S%f").replace(tzinfo=dt.UTC)
        if taken >= before:
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            continue  # another run removed it
        deleted += 1
    return deleted


async def apply_data_retention(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> dict[str, int]:
    """One run: every category, then the capture files. A failing category is logged at ERROR
    and retried by the next run; the others still run."""
    async with session_factory() as session:
        limits = await cutoff(session, settings)
    counts: dict[str, int] = {}
    errors: list[str] = []
    for category in CATEGORIES:
        try:
            counts[category.name] = await anonymize_category(
                session_factory, settings, category, limits
            )
        except Exception:
            logger.error("data retention failed", extra={"category": category.name}, exc_info=True)
            errors.append(category.name)
    try:
        counts["capture_files"] = delete_old_captures(settings.capture_dir, limits.ts)
    except OSError:
        logger.error("data retention failed", extra={"category": "capture_files"}, exc_info=True)
        errors.append("capture_files")
    logger.info(
        "data retention applied",
        extra={
            "cutoff": limits.ts.isoformat(),
            "cutoff_business_date": limits.date.isoformat(),
            "errors": errors,
            **{f"anonymized_{k}": v for k, v in counts.items()},
        },
    )
    return counts
