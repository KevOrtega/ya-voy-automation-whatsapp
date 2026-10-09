"""Recovery sweeps (docs/06 cron table): re-enqueue work whose arq job was lost."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ya_voy.config import Settings
from ya_voy.db.models import InboundEvent, OutboundMessage
from ya_voy.domain.enums import InboundEventStatus, OutboundStatus
from ya_voy.queue import enqueue_process_inbound_event, enqueue_send_outbound

logger = logging.getLogger(__name__)

SWEEP_BATCH = 500


async def recover_inbound_events(ctx: dict[str, Any]) -> int:
    """``received`` events older than ``INBOUND_RECOVERY_SECONDS`` -> ``process_inbound_event``."""
    settings: Settings = ctx["settings"]
    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    cutoff = func.now() - timedelta(seconds=settings.inbound_recovery_seconds)
    async with session_factory() as session:
        ids = (
            await session.scalars(
                select(InboundEvent.id)
                .where(
                    InboundEvent.status == InboundEventStatus.RECEIVED,
                    InboundEvent.received_at < cutoff,
                )
                .order_by(InboundEvent.id)
                .limit(SWEEP_BATCH)
            )
        ).all()
    for event_id in ids:
        await enqueue_process_inbound_event(ctx["redis"], event_id)
    if ids:
        logger.warning("re-enqueued stale inbound events", extra={"count": len(ids)})
    return len(ids)


async def recover_outbound_messages(ctx: dict[str, Any]) -> int:
    """``pending`` outbox rows older than ``OUTBOUND_RECOVERY_SECONDS`` -> ``send_outbound``.

    Rows waiting for a backoff are re-enqueued too, but ``send_outbound`` defers them again.
    """
    settings: Settings = ctx["settings"]
    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    cutoff = func.now() - timedelta(seconds=settings.outbound_recovery_seconds)
    async with session_factory() as session:
        ids = (
            await session.scalars(
                select(OutboundMessage.id)
                .where(
                    OutboundMessage.status == OutboundStatus.PENDING,
                    OutboundMessage.created_at < cutoff,
                )
                .order_by(OutboundMessage.id)
                .limit(SWEEP_BATCH)
            )
        ).all()
    for outbound_id in ids:
        await enqueue_send_outbound(ctx["redis"], outbound_id)
    if ids:
        logger.info("re-enqueued pending outbound messages", extra={"count": len(ids)})
    return len(ids)
