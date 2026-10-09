"""Order jobs: reactions (CU-04 to CU-07) and the ``alert_unclaimed_orders`` cron (CU-08, T15).

``handle_reaction`` is the entry point for stage 5b: it takes an already-normalized event, so it
does not depend on Evolution payloads or on ``@lid`` identity. Each call is one short
transaction; the outbox ids are enqueued after the commit.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ya_voy.config import Settings
from ya_voy.orders import claims, publication
from ya_voy.orders.events import ReactionAdded, ReactionRemoved
from ya_voy.orders.notify import OrderResult
from ya_voy.queue import enqueue_outbound_ids

logger = logging.getLogger(__name__)


async def apply_reaction(
    session: AsyncSession, settings: Settings, event: ReactionAdded | ReactionRemoved
) -> OrderResult:
    """In the caller's transaction (stage 5b: the one that marks the inbound event processed)."""
    if isinstance(event, ReactionAdded):
        return await claims.handle_reaction_added(session, settings, event)
    return await claims.handle_reaction_removed(session, settings, event)


async def handle_reaction(
    ctx: dict[str, Any], event: ReactionAdded | ReactionRemoved
) -> OrderResult:
    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    async with session_factory() as session, session.begin():
        result = await apply_reaction(session, ctx["settings"], event)
    await enqueue_outbound_ids(ctx["redis"], result.outbound_ids)
    logger.info(
        "reaction handled",
        extra={"order_id": result.order_id, "outcome": result.outcome},
    )
    return result


async def alert_unclaimed_orders(ctx: dict[str, Any]) -> int:
    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    async with session_factory() as session, session.begin():
        ids = await publication.alert_unclaimed_orders(session, ctx["settings"])
    await enqueue_outbound_ids(ctx["redis"], ids)
    return len(ids)
