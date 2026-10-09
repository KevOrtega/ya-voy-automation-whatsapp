"""Reactions to a group message that is not a known order post yet (docs/06 D-19, docs/04).

Evolution accepts the group post, and only then ``send_outbound`` records ``group_message_id``
(``publication.on_group_message_sent``) and commits. A fast courier's reaction can be processed in
between: it targets a WhatsApp id that no order has yet. Instead of discarding it, it is *parked*
(``parked_reactions``), findable by the id it references:

- When the post id is recorded, the hook applies the pending parked reactions for it **first**, in
  WhatsApp order (``reacted_at`` = ``messageTimestamp``, then arrival = ``inbound_event_id``, then
  parking order), in the same transaction that makes the id visible. Any reaction processed after
  that commit sees the id and goes through the usual atomic guard, so it cannot overtake an
  earlier parked one: the first to react wins.
- Each parked reaction also gets bounded deferred retries (``RETRY_DELAYS``); one that still does
  not match after the last retry is discarded and logged (a reaction on some other message, or on
  a post that never got its id recorded).

Closing the gap between "no order has this id" and "parked": a transaction-scoped advisory lock
on the target id (``lock_target``) is taken by every reaction and by the hook before it records
the id. A reaction either sees the committed id, or parks before the hook looks for parked rows.
The lock order is advisory lock, then the order row (claims and the hook alike).
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import exists, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ya_voy.config import Settings
from ya_voy.db.models import OutboundMessage, ParkedReaction
from ya_voy.domain.enums import ParkedReactionKind, ParkedReactionStatus
from ya_voy.orders import claims
from ya_voy.orders.events import ReactionAdded, ReactionRemoved
from ya_voy.orders.notify import OrderResult, now

logger = logging.getLogger(__name__)

# Seconds before each deferred retry of a parked reaction: 3 retries, given up ~5 s after
# parking. The post hook normally applies the reaction well before (the window is the time
# between Evolution's response and the commit of ``send_outbound``).
RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0, 2.0)

# First key of ``pg_advisory_xact_lock(int, int)`` for reaction targets (the second is
# ``hashtext(group_message_id)``; a hash collision only serializes two unrelated targets).
ADVISORY_NAMESPACE = 5_005

P = ParkedReactionStatus
Reaction = ReactionAdded | ReactionRemoved


async def lock_target(session: AsyncSession, group_message_id: str) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:ns, hashtext(:target))"),
        {"ns": ADVISORY_NAMESPACE, "target": group_message_id},
    )


async def _is_other_bot_message(session: AsyncSession, group_message_id: str) -> bool:
    """A bot message WhatsApp already has an id for that is not an order post (a reply such as
    ``group_order_assigned``): RN-12, nothing to wait for. Posts get their id in the outbox in the
    same transaction as ``group_message_id``, so a post is never mistaken for one of these."""
    return bool(
        await session.scalar(
            select(exists().where(OutboundMessage.wa_message_id == group_message_id))
        )
    )


async def _apply(
    session: AsyncSession, settings: Settings, event: Reaction, order_id: int
) -> OrderResult:
    if isinstance(event, ReactionAdded):
        return await claims.apply_added(session, settings, event, order_id)
    return await claims.apply_removed(session, settings, event, order_id)


async def handle_reaction(
    session: AsyncSession, settings: Settings, event: Reaction
) -> OrderResult:
    """Entry point for a normalized reaction, inside the caller's transaction."""
    await lock_target(session, event.group_message_id)
    order_id = await claims.order_id_for_post(session, event.group_message_id)
    if order_id is None:
        if await _is_other_bot_message(session, event.group_message_id):
            return OrderResult("ignored:not_an_order_post")
        parked_id = await _park(session, event)
        return OrderResult("parked", parked_id=parked_id)
    # Defensive: with the lock, parked rows for a known post are applied by the hook already.
    results = await apply_parked(session, settings, event.group_message_id, order_id)
    result = await _apply(session, settings, event, order_id)
    result.outbound_ids = [i for r in results for i in r.outbound_ids] + result.outbound_ids
    return result


async def _park(session: AsyncSession, event: Reaction) -> int | None:
    """Returns the new row's id, or ``None`` for a redelivered event already parked (it keeps
    its own retries; no second chain)."""
    added = isinstance(event, ReactionAdded)
    stmt = (
        insert(ParkedReaction)
        .values(
            group_message_id=event.group_message_id,
            kind=ParkedReactionKind.ADDED if added else ParkedReactionKind.REMOVED,
            courier_id=event.courier_id,
            emoji=event.emoji if isinstance(event, ReactionAdded) else None,
            inbound_event_id=event.inbound_event_id,
            reacted_at=event.reacted_at or await now(session),
        )
        .on_conflict_do_nothing(index_elements=["inbound_event_id"])
        .returning(ParkedReaction.id)
    )
    parked_id = await session.scalar(stmt)
    logger.info(
        "reaction parked until its group message is known",
        extra={"parked_reaction_id": parked_id, "courier_id": event.courier_id},
    )
    return parked_id


def _event(row: ParkedReaction) -> Reaction:
    if row.kind == ParkedReactionKind.ADDED:
        assert row.emoji is not None
        return ReactionAdded(
            row.group_message_id, row.courier_id, row.emoji, row.inbound_event_id, row.reacted_at
        )
    return ReactionRemoved(
        row.group_message_id, row.courier_id, row.inbound_event_id, row.reacted_at
    )


async def apply_parked(
    session: AsyncSession, settings: Settings, group_message_id: str, order_id: int
) -> list[OrderResult]:
    """Applies the pending parked reactions on ``group_message_id`` in WhatsApp order. The
    caller holds ``lock_target(group_message_id)``."""
    rows = list(
        await session.scalars(
            select(ParkedReaction)
            .where(
                ParkedReaction.group_message_id == group_message_id,
                ParkedReaction.status == P.PENDING,
            )
            .order_by(
                ParkedReaction.reacted_at,
                ParkedReaction.inbound_event_id.asc().nulls_last(),
                ParkedReaction.id,
            )
            .with_for_update()
        )
    )
    results = []
    at = await now(session) if rows else None
    for row in rows:
        result = await _apply(session, settings, _event(row), order_id)
        row.status = P.APPLIED
        row.resolved_at = at
        logger.info(
            "parked reaction applied",
            extra={"parked_reaction_id": row.id, "order_id": order_id, "outcome": result.outcome},
        )
        results.append(result)
    return results


async def retry_parked(
    session: AsyncSession, settings: Settings, parked_id: int
) -> tuple[OrderResult, dt.timedelta | None]:
    """One deferred retry: applies the reaction (and any earlier parked ones on the same post) if
    the post is known now; otherwise counts the attempt and returns the next delay, or discards it
    after the last one."""
    target = await session.scalar(
        select(ParkedReaction.group_message_id).where(ParkedReaction.id == parked_id)
    )
    if target is None:
        return OrderResult("ignored:missing"), None
    await lock_target(session, target)
    row = await session.scalar(
        select(ParkedReaction).where(ParkedReaction.id == parked_id).with_for_update()
    )
    assert row is not None
    if row.status != P.PENDING:  # the post hook (or an earlier retry) already resolved it
        return OrderResult(f"ignored:{row.status}"), None
    order_id = await claims.order_id_for_post(session, target)
    if order_id is not None:
        results = await apply_parked(session, settings, target, order_id)
        ids = [i for r in results for i in r.outbound_ids]
        return OrderResult("applied", order_id, ids), None
    row.attempts += 1
    if row.attempts >= len(RETRY_DELAYS):
        row.status = P.DISCARDED
        row.resolved_at = await now(session)
        logger.warning(
            "parked reaction discarded: its message never became a known order post",
            extra={
                "parked_reaction_id": row.id,
                "courier_id": row.courier_id,
                "attempts": row.attempts,
                "group_message_id": target,
            },
        )
        return OrderResult("discarded"), None
    return OrderResult("parked"), dt.timedelta(seconds=RETRY_DELAYS[row.attempts])
