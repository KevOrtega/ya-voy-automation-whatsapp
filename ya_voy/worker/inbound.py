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
        # TODO(stage-4b): classify the sender (ya_voy/identity: admin / courier / store,
        # auto-registration, @lid). For a store and Route.PRIVATE_TEXT, close this transaction
        # first (the LLM must not run while the event row is locked) and call
        #   drafts.handle_store_text(ctx, intake.StoreText(store_id=..., text=...,
        #       inbound_event_id=row.id, wa_message_id=..., quoted_wa_message_id=...),
        #       finalize=<mark the event processed if it is still `received`>)
        # then drafts.after_commit(ctx, result). PRIVATE_NON_TEXT -> store_unsupported_media.
        # Before intake, the RN-18 gate: schedule.repository.is_accepting_orders(); when closed,
        # reply messaging.schedule_texts.store_closed_reply() at most once per
        # OUT_OF_HOURS_REPLY_COOLDOWN_MINUTES (CU-10).
        # TODO(stage-5b): Route.GROUP_REACTION / GROUP_REACTION_REMOVAL -> verify the payload with
        # the real test number, resolve/upsert the courier from the participant (number or
        # @lid, docs/05 courier upsert and merge), then in THIS transaction
        #   result = await orders.apply_reaction(session, settings, ReactionAdded(
        #       group_message_id=event.reaction_target_id, courier_id=..., emoji=...,
        #       inbound_event_id=row.id))  (ReactionRemoved for a removal)
        # and outbound_ids += result.outbound_ids. Emoji-change payloads (a new reaction with no
        # removal, or a removal followed by a new reaction) must be confirmed there too.
        # TODO(stage-7b): an admin (ADMIN_PHONES, by number or @lid) -> the same pattern with
        #   schedules.handle_admin_text(ctx, schedule_admin.AdminText(admin_phone=...,
        #       reply_jid=..., text=..., inbound_event_id=row.id), finalize=...)
        # then schedules.after_commit(ctx, result).
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
    except Exception as exc:
        # Failed events are kept for inspection and are not swept (docs/06 sweeps only
        # `received`); stage 4 adds a manual reprocess command. Always visible at ERROR.
        error = f"{type(exc).__name__}: {exc}"[:ERROR_LIMIT]
        logger.error(
            "inbound event failed",
            extra=log_extra | {"error": error},
            exc_info=not isinstance(exc, PayloadError),
        )
        await _mark_failed(session_factory, inbound_event_id, error)
        return "failed"

    await enqueue_outbound_ids(ctx["redis"], outbound_ids)
    logger.info("inbound event processed", extra=log_extra | {"outcome": outcome})
    return outcome
