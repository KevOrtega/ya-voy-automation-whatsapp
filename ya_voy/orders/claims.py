"""Claims and releases (CU-04 to CU-07, CU-13 ``liberar``; docs/03 T10-T14, T18, T19).

Every decision is a conditional ``UPDATE`` on the order row (the ``WHERE`` is the guard): Postgres
serializes writers on the row, so exactly one claim wins (RN-01, RN-02) whatever the webhook
order. When the claim ``UPDATE`` returns no row, the order is read again only to choose the
message (T11 / T12 / T19), never to decide the assignment. Each function runs inside the
caller's short transaction; nothing here calls Evolution (messages go to the outbox).

Lock order: the order row first (the ``UPDATE``), then the daily report row
(``mark_report_for_correction``), as docs/03 requires.
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ya_voy.config import Settings
from ya_voy.db.models import Courier, Order, OrderEvent, Store
from ya_voy.domain.enums import ActorType, OrderEventType, OrderStatus
from ya_voy.messaging import catalog
from ya_voy.messaging import order_texts as texts
from ya_voy.orders.events import ReactionAdded, ReactionRemoved
from ya_voy.orders.notify import OrderResult, Outbox, add_event, courier_jid, key, now
from ya_voy.reports.service import mark_report_for_correction

logger = logging.getLogger(__name__)

S = OrderStatus


async def _already_applied(
    session: AsyncSession, order_id: int, inbound_event_id: int | None
) -> bool:
    """A redelivered event that already produced an order event (e.g. a job retried after its
    commit) does nothing, even if the order changed since (a stale "removed" must not release a
    later claim)."""
    if inbound_event_id is None:
        return False
    return bool(
        await session.scalar(
            select(
                exists().where(
                    OrderEvent.order_id == order_id,
                    OrderEvent.inbound_event_id == inbound_event_id,
                )
            )
        )
    )


async def _load(session: AsyncSession, order_id: int) -> tuple[Order, Store]:
    order = await session.get(Order, order_id, populate_existing=True)
    assert order is not None
    store = await session.get(Store, order.store_id)
    assert store is not None
    return order, store


async def order_id_for_post(session: AsyncSession, group_message_id: str) -> int | None:
    """RN-12: only reactions on an order's group post count (not on the bot's replies)."""
    return await session.scalar(select(Order.id).where(Order.group_message_id == group_message_id))


async def apply_added(
    session: AsyncSession, settings: Settings, event: ReactionAdded, order_id: int
) -> OrderResult:
    """T10 (claim), or T11 / T12 / T19 when the claim finds no row. ``order_id`` is the order
    whose post the reaction targets (``ya_voy.orders.parking.handle_reaction`` resolves it)."""
    courier = await session.get(Courier, event.courier_id)
    if courier is None:
        raise ValueError(f"courier {event.courier_id} does not exist")
    if courier.is_blocked:  # CU-04 3c
        return OrderResult("ignored:courier_blocked", order_id)
    if await _already_applied(session, order_id, event.inbound_event_id):
        return OrderResult("duplicate", order_id)

    at = await now(session)
    claimed = await session.scalar(
        update(Order)
        .where(Order.id == order_id, Order.courier_id.is_(None), Order.status == S.PUBLISHED)
        .values(
            courier_id=courier.id,
            last_courier_id=courier.id,
            status=S.ASSIGNED,
            assigned_at=at,
            updated_at=at,
        )
        .returning(Order.id)
        .execution_options(synchronize_session=False)
    )
    order, store = await _load(session, order_id)
    out = Outbox(session)
    if claimed is not None:
        await _claimed(session, settings, out, order, store, courier, event)
        return OrderResult("claimed", order_id, out.ids)

    if order.status == S.ASSIGNED and order.courier_id == courier.id:
        return OrderResult("ignored:same_courier", order_id)  # T12: emoji change
    if order.status == S.ASSIGNED:  # T11: lost the race (or an emoji change by a loser)
        await add_event(
            session,
            order,
            OrderEventType.CLAIM_REJECTED,
            ActorType.COURIER,
            actor_id=courier.id,
            courier_id=courier.id,
            from_status=S.ASSIGNED,
            to_status=S.ASSIGNED,
            inbound_event_id=event.inbound_event_id,
            data={"emoji": event.emoji, "assigned_courier_id": order.courier_id},
        )
        if settings.notify_loser_on_race:
            template = catalog.COURIER_ORDER_ALREADY_TAKEN
            await out.add(
                key(order.id, template, f"courier:{courier.id}"),
                template,
                courier_jid(courier),
                catalog.render(template, code=order.code),
                order_id=order.id,
            )
        return OrderResult("rejected:already_taken", order_id, out.ids)
    if order.status == S.CANCELLED:  # T19
        await add_event(
            session,
            order,
            OrderEventType.REACTION_ON_CANCELLED,
            ActorType.COURIER,
            actor_id=courier.id,
            courier_id=courier.id,
            from_status=S.CANCELLED,
            to_status=S.CANCELLED,
            inbound_event_id=event.inbound_event_id,
            data={"emoji": event.emoji},
        )
        if settings.notify_reaction_on_cancelled:
            template = catalog.COURIER_ORDER_NO_LONGER_AVAILABLE
            await out.add(
                key(order.id, template, f"courier:{courier.id}"),
                template,
                courier_jid(courier),
                catalog.render(template, code=order.code),
                order_id=order.id,
            )
        return OrderResult("ignored:cancelled", order_id, out.ids)
    return OrderResult(f"ignored:{order.status}", order_id)


async def _claimed(
    session: AsyncSession,
    settings: Settings,
    out: Outbox,
    order: Order,
    store: Store,
    courier: Courier,
    event: ReactionAdded,
) -> None:
    """CU-04 step 5: the winner, the store and the group (quoting the post)."""
    ev = await add_event(
        session,
        order,
        OrderEventType.CLAIMED,
        ActorType.COURIER,
        actor_id=courier.id,
        courier_id=courier.id,
        from_status=S.PUBLISHED,
        to_status=S.ASSIGNED,
        inbound_event_id=event.inbound_event_id,
        data={"emoji": event.emoji},
    )
    suffix = f"e{ev.id}"
    t = catalog.COURIER_ORDER_ASSIGNED
    await out.add(
        key(order.id, t, suffix),
        t,
        courier_jid(courier),
        catalog.render(t, code=order.code, order_details=texts.details(order, store)),
        order_id=order.id,
    )
    t = catalog.STORE_ORDER_ASSIGNED
    await out.add(
        key(order.id, t, suffix),
        t,
        store.wa_jid,
        catalog.render(t, code=order.code, courier_name=courier.display_name),
        order_id=order.id,
    )
    t = catalog.GROUP_ORDER_ASSIGNED
    await out.add(
        key(order.id, t, suffix),
        t,
        settings.couriers_group_jid,
        catalog.render(t, code=order.code, courier_name=courier.display_name),
        order_id=order.id,
        quoted_wa_message_id=order.group_message_id,
    )


async def _release(
    session: AsyncSession,
    settings: Settings,
    order_id: int,
    *,
    courier_id: int | None,
    at: dt.datetime,
) -> bool:
    """T13 (``courier_id`` given: only the assigned courier) or T18 (any assignment)."""
    guard = [Order.id == order_id, Order.status == S.ASSIGNED]
    if courier_id is not None:
        guard.append(Order.courier_id == courier_id)
    released = await session.scalar(
        update(Order)
        .where(*guard)
        .values(
            courier_id=None,
            status=S.PUBLISHED,
            assigned_at=None,
            unclaimed_alert_at=at + dt.timedelta(minutes=settings.unclaimed_alert_minutes),
            unclaimed_alert_sent_at=None,
            updated_at=at,
        )
        .returning(Order.id)
        .execution_options(synchronize_session=False)
    )
    return released is not None


async def _released_messages(
    settings: Settings,
    out: Outbox,
    order: Order,
    store: Store,
    courier: Courier,
    suffix: str,
    courier_template: str,
) -> None:
    t = catalog.STORE_ORDER_UNASSIGNED
    await out.add(
        key(order.id, t, suffix),
        t,
        store.wa_jid,
        catalog.render(t, code=order.code, courier_name=courier.display_name),
        order_id=order.id,
    )
    t = catalog.GROUP_ORDER_UNASSIGNED
    await out.add(
        key(order.id, t, suffix),
        t,
        settings.couriers_group_jid,
        catalog.render(t, code=order.code),
        order_id=order.id,
        quoted_wa_message_id=order.group_message_id,
    )
    await out.add(
        key(order.id, courier_template, suffix),
        courier_template,
        courier_jid(courier),
        catalog.render(courier_template, code=order.code),
        order_id=order.id,
    )


async def apply_removed(
    session: AsyncSession, settings: Settings, event: ReactionRemoved, order_id: int
) -> OrderResult:
    """T13 when the remover is the assigned courier; otherwise nothing (T14, RN-04, RN-06)."""
    if await _already_applied(session, order_id, event.inbound_event_id):
        return OrderResult("duplicate", order_id)
    courier = await session.get(Courier, event.courier_id)
    if courier is None:
        raise ValueError(f"courier {event.courier_id} does not exist")
    at = await now(session)
    if not await _release(session, settings, order_id, courier_id=courier.id, at=at):
        return OrderResult("ignored:not_assigned_to_remover", order_id)
    order, store = await _load(session, order_id)
    ev = await add_event(
        session,
        order,
        OrderEventType.RELEASED_BY_COURIER,
        ActorType.COURIER,
        actor_id=courier.id,
        courier_id=courier.id,
        from_status=S.ASSIGNED,
        to_status=S.PUBLISHED,
        inbound_event_id=event.inbound_event_id,
    )
    out = Outbox(session)
    await _released_messages(
        settings, out, order, store, courier, f"e{ev.id}", catalog.COURIER_ORDER_RELEASED
    )
    await mark_report_for_correction(session, order.business_date)  # CU-06 step 4
    return OrderResult("released", order_id, out.ids)


async def lock_order_by_code(session: AsyncSession, code: str) -> Order | None:
    return await session.scalar(select(Order).where(Order.code == code.upper()).with_for_update())


async def admin_command_error(
    out: Outbox,
    *,
    action: str,
    code: str,
    reason: str,
    admin_jid: str,
    request_key: str,
    status: str | None = None,
    order_id: int | None = None,
) -> None:
    t = catalog.ADMIN_COMMAND_ERROR
    await out.add(
        f"{t}:{request_key}",
        t,
        admin_jid,
        catalog.render(
            t,
            action_verb=catalog.ACTION_VERBS[action],
            code=code,
            reason=texts.command_error_reason(reason, status),
        ),
        order_id=order_id,
    )


async def release_by_admin(
    session: AsyncSession,
    settings: Settings,
    *,
    code: str,
    admin_jid: str,
    request_key: str,
    inbound_event_id: int | None = None,
) -> OrderResult:
    """CU-13 ``liberar {código}`` (T18), for a sender already recognized as an admin.

    TODO(stage-7b): call it from the admin's private text (identity by number or ``@lid``).
    """
    out = Outbox(session)
    order = await lock_order_by_code(session, code)
    code = code.upper()
    if order is None or order.status != S.ASSIGNED:
        if order is None:
            reason, status = "not_found", None
        elif order.status in (S.CANCELLED, S.EXPIRED):
            reason, status = "already", order.status
        else:
            reason, status = "not_assigned", None
        await admin_command_error(
            out,
            action="release",
            code=code,
            reason=reason,
            status=status,
            admin_jid=admin_jid,
            request_key=request_key,
            order_id=order.id if order else None,
        )
        return OrderResult(f"error:{reason}", order.id if order else None, out.ids)
    previous = await session.get(Courier, order.courier_id)
    assert previous is not None
    at = await now(session)
    released = await _release(session, settings, order.id, courier_id=None, at=at)
    assert released, "the row is locked and assigned"
    order, store = await _load(session, order.id)
    ev = await add_event(
        session,
        order,
        OrderEventType.RELEASED_BY_ADMIN,
        ActorType.ADMIN,
        courier_id=previous.id,
        from_status=S.ASSIGNED,
        to_status=S.PUBLISHED,
        inbound_event_id=inbound_event_id,
        data={"admin_jid": admin_jid},
    )
    suffix = f"e{ev.id}"
    await _released_messages(
        settings, out, order, store, previous, suffix, catalog.COURIER_ORDER_RELEASED_BY_ADMIN
    )
    t = catalog.ADMIN_COMMAND_OK
    await out.add(
        key(order.id, t, suffix),
        t,
        admin_jid,
        catalog.render(
            t, code=order.code, action_label=catalog.ACTION_LABELS["release"], courier_note=""
        ),
        order_id=order.id,
    )
    await mark_report_for_correction(session, order.business_date)
    return OrderResult("released", order.id, out.ids)
