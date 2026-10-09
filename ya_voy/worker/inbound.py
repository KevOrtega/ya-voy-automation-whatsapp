"""``process_inbound_event`` (docs/04 §1): one stored webhook event, processed exactly once.

Idempotency: the row is locked with ``SELECT ... FOR UPDATE`` and only a ``received`` row is
processed. A duplicate job (re-delivery, recovery sweep, two workers) waits for the lock and
then sees ``processed``/``ignored``/``failed`` and does nothing.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ya_voy.config import Settings
from ya_voy.db.models import InboundEvent
from ya_voy.domain.enums import InboundEventStatus
from ya_voy.messaging import catalog
from ya_voy.messaging.outbox import display_name_for_jid, enqueue_admin_alert
from ya_voy.queue import enqueue_outbound_ids
from ya_voy.whatsapp.payloads import InboundMessage, PayloadError, parse_webhook
from ya_voy.worker import ratelimit
from ya_voy.worker.routing import Route, is_private, route_event

logger = logging.getLogger(__name__)

ERROR_LIMIT = 1000
RATE_LIMITED = "rate_limited"


async def _rate_limit_alert(
    session: AsyncSession, settings: Settings, event: InboundMessage, now: datetime
) -> list[int]:
    """``admin_rate_limited_sender`` once per day and sender (CU-18), keyed in the outbox."""
    assert event.remote_jid
    day = now.astimezone(settings.zoneinfo).date().isoformat()
    name = (
        await display_name_for_jid(session, settings, event.remote_jid)
        or event.push_name
        or settings.store_fallback_name
    )
    return await enqueue_admin_alert(
        session,
        settings,
        template_key=catalog.ADMIN_RATE_LIMITED_SENDER,
        idempotency_base=f"{catalog.ADMIN_RATE_LIMITED_SENDER}:{day}:{event.remote_jid}",
        variables={"sender_name": name},
    )


async def _process_locked(
    session: AsyncSession, ctx: dict[str, Any], row: InboundEvent
) -> tuple[str, list[int]]:
    settings: Settings = ctx["settings"]
    now = await session.scalar(select(func.now()))
    assert now is not None
    row.attempts += 1
    event = parse_webhook(row.payload)
    route = route_event(event, couriers_group_jid=settings.couriers_group_jid)
    outbound_ids: list[int] = []

    if is_private(route):
        assert isinstance(event, InboundMessage) and event.remote_jid
        within = await ratelimit.hit(
            ctx["redis"], event.remote_jid, settings.sender_rate_limit_per_minute
        )
        if not within:
            row.status = InboundEventStatus.IGNORED
            row.error = RATE_LIMITED
            row.processed_at = now
            outbound_ids = await _rate_limit_alert(session, settings, event, now)
            return RATE_LIMITED, outbound_ids

    if route is Route.UNHANDLED:
        row.status = InboundEventStatus.IGNORED
    else:
        # TODO(stage 4+): call the handler for `route` here, inside this transaction, and
        # collect the outbound ids it inserts.
        row.status = InboundEventStatus.PROCESSED
    row.processed_at = now
    return str(route), outbound_ids


async def _mark_failed(
    session_factory: async_sessionmaker[AsyncSession], inbound_event_id: int, error: str
) -> None:
    async with session_factory() as session, session.begin():
        await session.execute(
            update(InboundEvent)
            .where(
                InboundEvent.id == inbound_event_id,
                InboundEvent.status == InboundEventStatus.RECEIVED,
            )
            .values(
                status=InboundEventStatus.FAILED,
                attempts=InboundEvent.attempts + 1,
                error=error[:ERROR_LIMIT],
                processed_at=func.now(),
            )
        )


async def process_inbound_event(ctx: dict[str, Any], inbound_event_id: int) -> str:
    """Returns what happened (for logs and tests): the route, ``rate_limited`` or ``skipped:*``."""
    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    log_extra = {"inbound_event_id": inbound_event_id}
    try:
        async with session_factory() as session, session.begin():
            row = await session.scalar(
                select(InboundEvent).where(InboundEvent.id == inbound_event_id).with_for_update()
            )
            if row is None:
                logger.warning("inbound event not found", extra=log_extra)
                return "skipped:missing"
            if row.status != InboundEventStatus.RECEIVED:
                logger.info(
                    "inbound event already handled", extra=log_extra | {"status": row.status}
                )
                return f"skipped:{row.status}"
            outcome, outbound_ids = await _process_locked(session, ctx, row)
    except PayloadError as exc:
        logger.warning("stored inbound payload is not parseable", extra=log_extra)
        await _mark_failed(session_factory, inbound_event_id, f"PayloadError: {exc}")
        return "failed"
    except Exception as exc:
        logger.exception("inbound event processing failed", extra=log_extra)
        # TODO(stage 4): retry policy for transient failures (LLM, DB); failed rows are kept
        # for inspection and are not swept (docs/06 sweeps only `received`).
        await _mark_failed(session_factory, inbound_event_id, f"{type(exc).__name__}: {exc}")
        return "failed"

    await enqueue_outbound_ids(ctx["redis"], outbound_ids)
    logger.info("inbound event processed", extra=log_extra | {"outcome": outcome})
    return outcome
