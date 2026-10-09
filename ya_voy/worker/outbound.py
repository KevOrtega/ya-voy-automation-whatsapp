"""``send_outbound`` (docs/04 §9): deliver one outbox row through Evolution.

- The row is taken with ``FOR UPDATE SKIP LOCKED``; a concurrent duplicate job skips it, and only
  a ``pending`` row is sent, so a re-delivered job never sends twice.
- FIFO per recipient: a row waits while an earlier row for the same ``recipient_jid`` is still
  ``pending``/``sending``. A ``failed`` earlier row does not block the queue.
- Global pacing: at most one send every ``OUTBOUND_MIN_INTERVAL_MS`` (``SET NX PX`` in Redis).
- Transient errors (network/timeout, 5xx, 408, 429): back off (``2 ** attempts`` s, capped) and
  retry up to ``OUTBOUND_MAX_ATTEMPTS``. Permanent errors (any other 4xx, unusable 2xx body,
  missing report file) go straight to ``failed``. Either way, ``failed`` sends
  ``admin_outbound_failed`` to every admin (never for a failed alert itself).

The row lock is held during the Evolution call, so ``sending`` is only visible inside that
transaction; a crash rolls back to ``pending`` and the recovery sweep re-enqueues it.
Waiting/backoff is done by re-enqueueing the job with ``_defer_by``. The backoff deadline is
kept in Redis (``outbound:backoff:{id}``) so the sweep cannot shortcut it.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ya_voy.config import Settings
from ya_voy.db.models import Message, OutboundMessage, ReportRevision
from ya_voy.domain.enums import MessageDirection, MessageKind, OutboundMessageType, OutboundStatus
from ya_voy.messaging import catalog
from ya_voy.messaging.outbox import enqueue_admin_alert, recipient_name
from ya_voy.queue import enqueue_outbound_ids, enqueue_send_outbound
from ya_voy.whatsapp.client import EvolutionClient, EvolutionError, SentMessage

logger = logging.getLogger(__name__)

INTERVAL_LOCK_KEY = "outbound:interval"
BACKOFF_KEY = "outbound:backoff:{id}"
FIFO_RECHECK = timedelta(seconds=1)
BACKOFF_BASE_SECONDS = 2
BACKOFF_MAX_SECONDS = 300
ERROR_LIMIT = 1000
XLSX_MIMETYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def backoff_delay(attempts: int) -> timedelta:
    """Delay before retry number ``attempts + 1``: 2, 4, 8, ... seconds, capped at 5 minutes."""
    return timedelta(seconds=min(BACKOFF_BASE_SECONDS**attempts, BACKOFF_MAX_SECONDS))


@dataclass
class _Outcome:
    result: str
    defer: timedelta | None = None
    alert_ids: list[int] = field(default_factory=list)


async def _deliver(
    session: AsyncSession, evolution: EvolutionClient, row: OutboundMessage
) -> SentMessage:
    kind = OutboundMessageType(row.message_type)
    if kind is OutboundMessageType.TEXT:
        return await evolution.send_text(
            row.recipient_jid, row.body, quoted_wa_message_id=row.quoted_wa_message_id
        )
    if kind is OutboundMessageType.DOCUMENT:
        revision = await session.get(ReportRevision, row.report_revision_id)
        if revision is None or revision.file_content is None:
            raise EvolutionError(
                "sendMedia", "report revision has no file content", retryable=False
            )
        return await evolution.send_document(
            row.recipient_jid,
            media_base64=base64.b64encode(revision.file_content).decode(),
            file_name=revision.file_name or f"reporte-{revision.id}.xlsx",
            mimetype=XLSX_MIMETYPE,
            caption=row.body,
        )
    assert row.target_wa_message_id is not None  # CHECK outbound_messages_target_for_delete_edit
    if kind is OutboundMessageType.DELETE:
        return await evolution.delete_for_everyone(row.recipient_jid, row.target_wa_message_id)
    return await evolution.update_message(row.recipient_jid, row.target_wa_message_id, row.body)


_HISTORY_KINDS = {
    OutboundMessageType.TEXT: MessageKind.TEXT,
    OutboundMessageType.DOCUMENT: MessageKind.DOCUMENT,
}


def _record_sent_message(session: AsyncSession, row: OutboundMessage) -> None:
    """Conversation history (RF-58) for new messages; deletes and edits are not new messages."""
    kinds = {
        OutboundMessageType.TEXT: MessageKind.TEXT,
        OutboundMessageType.DOCUMENT: MessageKind.DOCUMENT,
    }
    kind = kinds.get(OutboundMessageType(row.message_type))
    if kind is None:
        return
    session.add(
        Message(
            direction=MessageDirection.OUT,
            chat_jid=row.recipient_jid,
            wa_message_id=row.wa_message_id,
            quoted_wa_message_id=row.quoted_wa_message_id,
            kind=kind,
            body=row.body,
            order_id=row.order_id,
            outbound_message_id=row.id,
        )
    )


async def _send_locked(
    session: AsyncSession, ctx: dict[str, Any], row: OutboundMessage
) -> _Outcome:
    settings: Settings = ctx["settings"]
    redis = ctx["redis"]

    backoff_ms = await redis.pttl(BACKOFF_KEY.format(id=row.id))
    if backoff_ms > 0:
        return _Outcome("backoff", defer=timedelta(milliseconds=backoff_ms))

    earlier_pending = await session.scalar(
        select(
            exists().where(
                OutboundMessage.recipient_jid == row.recipient_jid,
                OutboundMessage.id < row.id,
                OutboundMessage.status.in_([OutboundStatus.PENDING, OutboundStatus.SENDING]),
            )
        )
    )
    if earlier_pending:
        return _Outcome("waiting_fifo", defer=FIFO_RECHECK)

    interval_ms = settings.outbound_min_interval_ms
    if interval_ms > 0 and not await redis.set(INTERVAL_LOCK_KEY, row.id, px=interval_ms, nx=True):
        wait_ms = max(await redis.pttl(INTERVAL_LOCK_KEY), 1)
        return _Outcome("throttled", defer=timedelta(milliseconds=wait_ms))

    row.status = OutboundStatus.SENDING
    row.attempts += 1
    await session.flush()
    try:
        sent = await _deliver(session, ctx["evolution"], row)
    except EvolutionError as exc:
        row.last_error = str(exc)[:ERROR_LIMIT]
        if exc.retryable and row.attempts < settings.outbound_max_attempts:
            row.status = OutboundStatus.PENDING
            delay = backoff_delay(row.attempts)
            await redis.set(BACKOFF_KEY.format(id=row.id), 1, px=int(delay.total_seconds() * 1000))
            return _Outcome("retry", defer=delay)
        row.status = OutboundStatus.FAILED
        if not exc.retryable:
            logger.error(
                "outbound message failed permanently",
                extra={"outbound_message_id": row.id, "error": row.last_error},
            )
        return _Outcome("failed", alert_ids=await _failure_alert(session, settings, row))

    row.status = OutboundStatus.SENT
    row.wa_message_id = sent.wa_message_id
    row.sent_at = await session.scalar(select(func.now()))
    _record_sent_message(session, row)
    return _Outcome("sent")


async def _failure_alert(
    session: AsyncSession, settings: Settings, row: OutboundMessage
) -> list[int]:
    if row.template_key == catalog.ADMIN_OUTBOUND_FAILED:
        # Never alert about a failed alert (no loops); it stays in the logs and the table.
        logger.error("admin alert could not be sent", extra={"outbound_message_id": row.id})
        return []
    return await enqueue_admin_alert(
        session,
        settings,
        template_key=catalog.ADMIN_OUTBOUND_FAILED,
        idempotency_base=f"{catalog.ADMIN_OUTBOUND_FAILED}:{row.id}",
        variables={
            "template_key": row.template_key,
            "recipient_name": await recipient_name(session, settings, row.recipient_jid),
            "attempts": row.attempts,
        },
    )


async def send_outbound(ctx: dict[str, Any], outbound_message_id: int) -> str:
    """Returns what happened: ``sent``, ``retry``, ``failed``, ``waiting_fifo``, ``throttled``,
    ``backoff`` or ``skipped:*``."""
    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    log_extra = {"outbound_message_id": outbound_message_id}
    async with session_factory() as session, session.begin():
        row = await session.scalar(
            select(OutboundMessage)
            .where(OutboundMessage.id == outbound_message_id)
            .with_for_update(skip_locked=True)
        )
        if row is None:
            # Missing, or another job holds it right now.
            return "skipped:locked_or_missing"
        if row.status != OutboundStatus.PENDING:
            return f"skipped:{row.status}"
        outcome = await _send_locked(session, ctx, row)
        log_extra |= {"attempts": row.attempts, "template_key": row.template_key}

    if outcome.defer is not None:
        await enqueue_send_outbound(ctx["redis"], outbound_message_id, defer_by=outcome.defer)
    await enqueue_outbound_ids(ctx["redis"], outcome.alert_ids)
    level = logging.WARNING if outcome.result in ("retry", "failed") else logging.INFO
    logger.log(level, "outbound message %s", outcome.result, extra=log_extra)
    return outcome.result
