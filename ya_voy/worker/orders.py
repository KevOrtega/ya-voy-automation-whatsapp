"""Order jobs: reactions (CU-04 to CU-07) and the ``alert_unclaimed_orders`` cron (CU-08, T15).

``handle_reaction`` is the entry point for stage 5b: it takes an already-normalized event, so it
does not depend on Evolution payloads or on ``@lid`` identity. Each call is one short
transaction; the outbox ids (and the first retry of a parked reaction) are enqueued after the
commit. ``retry_parked_reaction`` is the bounded deferred retry of ``ya_voy.orders.parking``.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ya_voy.config import Settings
from ya_voy.orders import parking, publication
from ya_voy.orders.events import ReactionAdded, ReactionRemoved
from ya_voy.orders.notify import OrderResult
from ya_voy.queue import enqueue_outbound_ids, enqueue_retry_parked_reaction

logger = logging.getLogger(__name__)


async def apply_reaction(
    session: AsyncSession, settings: Settings, event: ReactionAdded | ReactionRemoved
) -> OrderResult:
    """In the caller's transaction (stage 5b: the one that marks the inbound event processed)."""
    return await parking.handle_reaction(session, settings, event)


async def handle_reaction(
    ctx: dict[str, Any], event: ReactionAdded | ReactionRemoved
) -> OrderResult:
    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    async with session_factory() as session, session.begin():
        result = await apply_reaction(session, ctx["settings"], event)
    await enqueue_outbound_ids(ctx["redis"], result.outbound_ids)
    await enqueue_parked_retry(ctx, result)
    logger.info(
        "reaction handled",
        extra={"order_id": result.order_id, "outcome": result.outcome},
    )
    return result


async def enqueue_parked_retry(ctx: dict[str, Any], result: OrderResult) -> None:
    """After the commit that parked a reaction: its first deferred retry."""
    if result.parked_id is not None:
        await enqueue_retry_parked_reaction(
            ctx["redis"],
            result.parked_id,
            defer_by=dt.timedelta(seconds=parking.RETRY_DELAYS[0]),
        )


async def retry_parked_reaction(ctx: dict[str, Any], parked_id: int) -> str:
    """arq job: one bounded retry of a parked reaction (``ya_voy.orders.parking``)."""
    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    async with session_factory() as session, session.begin():
        result, next_delay = await parking.retry_parked(session, ctx["settings"], parked_id)
    await enqueue_outbound_ids(ctx["redis"], result.outbound_ids)
    if next_delay is not None:
        await enqueue_retry_parked_reaction(ctx["redis"], parked_id, defer_by=next_delay)
    return result.outcome


async def alert_unclaimed_orders(ctx: dict[str, Any]) -> int:
    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    async with session_factory() as session, session.begin():
        ids = await publication.alert_unclaimed_orders(session, ctx["settings"])
    await enqueue_outbound_ids(ctx["redis"], ids)
    return len(ids)
