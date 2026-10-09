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

Identities (``stores``, ``couriers``) with no activity inside the window are anonymized last:
phone, name, every JID and the ``lid`` are cleared (the name becomes ``STORE_FALLBACK_NAME`` /
``COURIER_FALLBACK_NAME``). "Activity" is any row that references the identity and is still not
anonymized (rows inside the window never are; older ones only while a live flow guards them),
plus the identity's own timestamps. ``ADMIN_PHONES`` and blocked identities are never anonymized.
A later message from the same person finds no row (its identifiers are ``NULL``) and is
registered as a new identity; old orders and reports keep pointing at the anonymized one. JIDs
and phones in rows past the window are scrubbed too, except group JIDs and ``ADMIN_PHONES``.

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

from sqlalchemy import Text, bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ya_voy.config import Settings
from ya_voy.db.models import sql_in
from ya_voy.domain.enums import PENDING_STATUSES
from ya_voy.schedule import repository as schedule_repository
from ya_voy.schedule.calendar import business_date
from ya_voy.whatsapp.jids import jid_for_phone

logger = logging.getLogger(__name__)

# Order statuses that hold back the daily report (docs/03): never anonymized, nor their data.
_PENDING_ORDER = "('draft', 'awaiting_confirmation', 'published')"
_ORDER_IS_PENDING = (
    f"EXISTS (SELECT 1 FROM orders o WHERE o.id = t.order_id AND o.status IN {_PENDING_ORDER})"
)


_EMPTY = "''"  # NOT NULL text columns are emptied instead of nulled


def _keep_jid(column: str) -> str:
    """Group JIDs (not a person) and the admins' own JIDs survive; any other JID is personal."""
    return f"({column} LIKE '%@g.us' OR {column} = ANY(:admin_jids))"


def _scrub_jid(column: str, empty: str = "NULL") -> str:
    return f"{column} = CASE WHEN {_keep_jid('t.' + column)} THEN t.{column} ELSE {empty} END"


# Courier names inside a report revision become the fallback name (ids and statuses stay).
_SNAPSHOT_NAMES = """(
    SELECT COALESCE(jsonb_object_agg(e.key, e.value || CASE
               WHEN e.value->>'courier_name' IS NULL THEN '{}'::jsonb
               ELSE jsonb_build_object('courier_name', CAST(:courier_name AS text)) END),
           '{}'::jsonb)
      FROM jsonb_each(t.snapshot) e)"""
_CHANGE_NAMES = """(
    SELECT COALESCE(jsonb_agg(c.value
               || CASE WHEN c.value->>'from' IS NULL THEN '{}'::jsonb
                       ELSE jsonb_build_object('from', CAST(:courier_name AS text)) END
               || CASE WHEN c.value->>'to' IS NULL THEN '{}'::jsonb
                       ELSE jsonb_build_object('to', CAST(:courier_name AS text)) END
               ORDER BY c.ordinality), '[]'::jsonb)
      FROM jsonb_array_elements(t.changes) WITH ORDINALITY c)"""

# An identity row is still referenced by live or recent data (docs/06, "activity").
_NOT_ANONYMIZED = "x.anonymized_at IS NULL"
_STORE_ACTIVE = f"""(
    t.created_at >= :cutoff_ts
    OR COALESCE(t.last_message_at >= :cutoff_ts, false)  -- NULL: never wrote
    OR EXISTS (SELECT 1 FROM orders x WHERE x.store_id = t.id AND {_NOT_ANONYMIZED})
    OR EXISTS (SELECT 1 FROM messages x WHERE x.store_id = t.id AND {_NOT_ANONYMIZED})
    OR EXISTS (SELECT 1 FROM inbound_events x
                WHERE x.remote_jid IN (t.wa_jid, t.lid) AND {_NOT_ANONYMIZED})
    OR EXISTS (SELECT 1 FROM outbound_messages x
                WHERE x.recipient_jid IN (t.wa_jid, t.lid) AND {_NOT_ANONYMIZED}))"""
_COURIER_ACTIVE = f"""(
    t.first_seen_at >= :cutoff_ts
    OR t.last_seen_at >= :cutoff_ts
    OR EXISTS (SELECT 1 FROM orders x
                WHERE (x.courier_id = t.id OR x.last_courier_id = t.id) AND {_NOT_ANONYMIZED})
    OR EXISTS (SELECT 1 FROM order_events x
                WHERE (x.courier_id = t.id OR (x.actor_type = 'courier' AND x.actor_id = t.id))
                  AND {_NOT_ANONYMIZED})
    OR EXISTS (SELECT 1 FROM messages x WHERE x.courier_id = t.id AND {_NOT_ANONYMIZED})
    OR EXISTS (SELECT 1 FROM parked_reactions x
                WHERE x.courier_id = t.id
                  AND (x.status = 'pending' OR x.created_at >= :cutoff_ts))
    OR EXISTS (SELECT 1 FROM inbound_events x
                WHERE (x.participant IN (t.lid, t.phone_jid)
                       OR x.remote_jid IN (t.lid, t.phone_jid))
                  AND {_NOT_ANONYMIZED})
    OR EXISTS (SELECT 1 FROM outbound_messages x
                WHERE x.recipient_jid IN (t.lid, t.phone_jid) AND {_NOT_ANONYMIZED}))"""


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
        f"""payload = '{{}}'::jsonb, error = NULL,
            {_scrub_jid("remote_jid")}, {_scrub_jid("participant")}""",
    ),
    Category(
        "messages",
        "messages",
        f"t.created_at < :cutoff_ts AND (t.order_id IS NULL OR NOT {_ORDER_IS_PENDING})",
        f"body = NULL, {_scrub_jid('chat_jid', _EMPTY)}, {_scrub_jid('sender_jid')}",
    ),
    Category(
        "outbound_messages",
        "outbound_messages",
        "t.created_at < :cutoff_ts AND t.status IN ('sent', 'failed')",
        # Keys may embed a sender's JID (admin_rate_limited_sender); none is reused past the window.
        f"""body = '', last_error = NULL, idempotency_key = 'anonymized:' || t.id,
            {_scrub_jid("recipient_jid", _EMPTY)}""",
    ),
    Category(
        "schedule_overrides",
        "schedule_overrides",
        """t.created_at < :cutoff_ts
            AND t.status <> 'pending_confirmation'
            AND NOT (t.status = 'active'
                     AND (t.ends_at IS NULL OR t.ends_at >= :cutoff_ts)
                     AND (t.business_date IS NULL OR t.business_date >= :cutoff_date))""",
        """source_text = '', parsed = '{}'::jsonb, reason = NULL,
           created_by = CASE WHEN t.created_by = ANY(:admin_phones)
                             THEN t.created_by ELSE '' END""",
    ),
    Category(
        "report_revisions",
        "report_revisions",
        """EXISTS (
                SELECT 1 FROM daily_reports r
                 WHERE r.id = t.daily_report_id
                   AND r.report_date < :cutoff_date
                   AND r.status IN ('sent', 'failed'))
            AND NOT EXISTS (
                SELECT 1 FROM outbound_messages o
                 WHERE o.report_revision_id = t.id AND o.status IN ('pending', 'sending'))""",
        # The .xlsx lists addresses and phones. ``file_name`` is only "reporte-ya-voy-<date>.xlsx".
        f"file_content = NULL, snapshot = {_SNAPSHOT_NAMES}, changes = {_CHANGE_NAMES}",
    ),
    # Identities last: they wait until every row referencing them was anonymized above.
    Category(
        "stores",
        "stores",
        f"""NOT t.is_blocked
            AND NOT COALESCE(t.phone = ANY(:admin_phones) OR t.wa_jid = ANY(:admin_jids), false)
            AND NOT {_STORE_ACTIVE}""",
        "wa_jid = NULL, phone = NULL, lid = NULL, display_name = :store_name, updated_at = :now",
    ),
    Category(
        "couriers",
        "couriers",
        f"""NOT t.is_blocked
            AND NOT COALESCE(t.phone = ANY(:admin_phones) OR t.phone_jid = ANY(:admin_jids), false)
            AND NOT {_COURIER_ACTIVE}""",
        """lid = NULL, phone_jid = NULL, phone = NULL, display_name = :courier_name,
           updated_at = :now""",
    ),
)
_ARRAYS = ("admin_phones", "admin_jids")

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


def run_params(settings: Settings, limits: Cutoff) -> dict[str, object]:
    return {
        "now": limits.now,
        "cutoff_ts": limits.ts,
        "cutoff_date": limits.date,
        "batch": settings.data_retention_batch_size,
        "admin_phones": list(settings.admin_phones),
        "admin_jids": [jid_for_phone(p) for p in settings.admin_phones],
        "store_name": settings.store_fallback_name,
        "courier_name": settings.courier_fallback_name,
    }


async def anonymize_batch(
    session: AsyncSession, category: Category, params: dict[str, object]
) -> int:
    sql = _batch_sql(category)
    used = {k: v for k, v in params.items() if f":{k}" in sql}
    statement = text(sql).bindparams(
        *(bindparam(k, type_=ARRAY(Text)) for k in _ARRAYS if k in used)
    )
    rows = await session.execute(statement, used)
    return len(rows.all())


async def anonymize_category(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    category: Category,
    limits: Cutoff,
) -> int:
    """Batches until one comes back short (done, or the rest is locked by someone else)."""
    total = 0
    params = run_params(settings, limits)
    batch_size = settings.data_retention_batch_size
    while True:
        async with session_factory() as session, session.begin():
            done = await anonymize_batch(session, category, params)
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
