"""Outbox rows of the order lifecycle, with deterministic idempotency keys (docs/05).

Keys:

- a transition's messages: ``order:{id}:{template_key}:e{order_event_id}`` (the event that the
  transition recorded), so a release followed by a new claim notifies again while a re-run of the
  same transition cannot (it does not happen twice: the guards are conditional ``UPDATE``\\s);
- once per courier and order (T11, T19): ``order:{id}:{template_key}:courier:{courier_id}``;
- group deletes: ``order:{id}:delete:{wa_message_id}`` (docs/05 example);
- replies to a command: ``{template_key}:{request_key}`` (``request_key`` names the inbound
  message, e.g. ``inbound:42``).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ya_voy.db.models import Courier, Order, OrderEvent
from ya_voy.db.queries import enqueue_outbound_message
from ya_voy.domain.enums import ActorType, OrderEventType, OutboundMessageType
from ya_voy.schedule import repository as schedule_repository


@dataclass
class OrderResult:
    """What a command or event did. Enqueue ``outbound_ids`` after the commit."""

    outcome: str
    order_id: int | None = None
    outbound_ids: list[int] = field(default_factory=list)
    # A reaction parked for a post id not known yet: enqueue its first deferred retry after the
    # commit (``worker.orders.enqueue_parked_retry``).
    parked_id: int | None = None


async def now(session: AsyncSession) -> dt.datetime:
    # Through the module attribute so tests can freeze the clock (as in schedules and reports).
    return await schedule_repository.db_now(session)


def courier_jid(courier: Courier) -> str:
    """Private chat of a courier. TODO(stage-5b): confirm with the real number that Evolution
    delivers private messages to an ``@lid`` (couriers known only by ``lid``)."""
    jid = courier.phone_jid or courier.lid
    assert jid is not None  # CHECK couriers_has_identity
    return jid


def key(order_id: int, template_key: str, suffix: str) -> str:
    return f"order:{order_id}:{template_key}:{suffix}"


async def add_event(
    session: AsyncSession,
    order: Order,
    event_type: OrderEventType,
    actor_type: ActorType,
    *,
    actor_id: int | None = None,
    courier_id: int | None = None,
    from_status: str | None = None,
    to_status: str | None = None,
    inbound_event_id: int | None = None,
    data: dict[str, Any] | None = None,
) -> OrderEvent:
    event = OrderEvent(
        order_id=order.id,
        event_type=event_type,
        actor_type=actor_type,
        actor_id=actor_id,
        courier_id=courier_id,
        from_status=from_status,
        to_status=to_status,
        inbound_event_id=inbound_event_id,
        data=data or {},
    )
    session.add(event)
    await session.flush()
    return event


class Outbox:
    """Collects the ids of the rows it adds (``None`` from a duplicate key is skipped)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.ids: list[int] = []

    async def add(
        self,
        idempotency_key: str,
        template_key: str,
        recipient_jid: str,
        body: str = "",
        *,
        order_id: int | None = None,
        quoted_wa_message_id: str | None = None,
        target_wa_message_id: str | None = None,
        message_type: OutboundMessageType = OutboundMessageType.TEXT,
    ) -> int | None:
        new_id = await enqueue_outbound_message(
            self.session,
            idempotency_key=idempotency_key,
            template_key=template_key,
            recipient_jid=recipient_jid,
            message_type=message_type,
            body=body,
            order_id=order_id,
            quoted_wa_message_id=quoted_wa_message_id,
            target_wa_message_id=target_wa_message_id,
        )
        if new_id is not None:
            self.ids.append(new_id)
        return new_id

    def extend(self, ids: list[int]) -> None:
        self.ids.extend(ids)
