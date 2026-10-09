"""Publication (CU-03, docs/03 T06/T07), the group-post hooks of ``send_outbound`` and the
unclaimed-order alert (CU-08, T15).

``group_message_id`` is the WhatsApp id of the group post, known only once Evolution accepted it:
:func:`on_group_message_sent` stores it, applies first the reactions parked while it was unknown
(``ya_voy.orders.parking``, D-19) and starts the unclaimed-alert period then (CU-03 4a). The
hook locks the order row, so it is serialized with a cancellation: a group message of a
cancelled order that is sent late (it was still in the outbox when the order was cancelled) gets
its delete right away.
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ya_voy.config import Settings
from ya_voy.db.models import Order, OutboundMessage, Store
from ya_voy.db.queries import next_order_code
from ya_voy.domain.enums import ActorType, OrderEventType, OrderStatus, OutboundMessageType
from ya_voy.messaging import catalog
from ya_voy.messaging import order_texts as texts
from ya_voy.messaging.outbox import enqueue_admin_alert
from ya_voy.orders import parking
from ya_voy.orders.cancellation import enqueue_group_delete
from ya_voy.orders.notify import OrderResult, Outbox, add_event, key, now
from ya_voy.schedule import repository as schedule_repository

logger = logging.getLogger(__name__)

S = OrderStatus


async def publish(
    session: AsyncSession,
    settings: Settings,
    order: Order,
    store: Store,
    *,
    request_key: str,
    inbound_event_id: int | None = None,
) -> OrderResult:
    """T06 / T07 for an ``awaiting_confirmation`` order the caller locked ``FOR UPDATE``.

    The business-hours check runs right before, in the same transaction (docs/03).
    """
    at = await now(session)
    out = Outbox(session)
    decision = await schedule_repository.is_accepting_orders(session, settings, at)
    if not decision.accepting:  # T07 (RN-18): nothing changes, the order expires on its own
        hours = await schedule_repository.todays_hours(session, settings, at)
        t = catalog.STORE_ORDER_NOT_SENT_CLOSED
        await out.add(
            key(order.id, t, request_key),
            t,
            store.wa_jid,
            catalog.render(t, reason_text=texts.not_sent_reason_text(decision, settings, hours)),
            order_id=order.id,
        )
        return OrderResult("not_sent_closed", order.id, out.ids)

    code = await next_order_code(session, settings.order_code_prefix)
    published = await session.scalar(
        update(Order)
        .where(
            Order.id == order.id,
            Order.store_id == store.id,
            Order.status == S.AWAITING_CONFIRMATION,
        )
        .values(
            status=S.PUBLISHED,
            code=code,
            confirmed_at=at,
            published_at=at,
            draft_expires_at=None,
            unclaimed_alert_at=at + dt.timedelta(minutes=settings.unclaimed_alert_minutes),
            updated_at=at,
        )
        .returning(Order.id)
        .execution_options(synchronize_session=False)
    )
    assert published is not None, "the row is locked FOR UPDATE as awaiting_confirmation"
    await session.refresh(order)
    for event_type, from_status in (
        (OrderEventType.CONFIRMED, S.AWAITING_CONFIRMATION),
        (OrderEventType.PUBLISHED, S.AWAITING_CONFIRMATION),
    ):
        await add_event(
            session,
            order,
            event_type,
            ActorType.STORE,
            actor_id=store.id,
            from_status=from_status,
            to_status=S.PUBLISHED,
            inbound_event_id=inbound_event_id if event_type == OrderEventType.CONFIRMED else None,
        )
    t = catalog.GROUP_ORDER_POST
    await out.add(
        key(order.id, t, "post"),
        t,
        settings.couriers_group_jid,
        catalog.render(t, code=code, order_details=texts.details(order, store)),
        order_id=order.id,
    )
    t = catalog.STORE_ORDER_PUBLISHED
    await out.add(
        key(order.id, t, "post"),
        t,
        store.wa_jid,
        catalog.render(t, code=code),
        order_id=order.id,
    )
    return OrderResult("published", order.id, out.ids)


def _is_group_order_message(settings: Settings, row: OutboundMessage) -> bool:
    return row.order_id is not None and row.recipient_jid == settings.couriers_group_jid


async def on_group_message_sent(
    session: AsyncSession, settings: Settings, row: OutboundMessage
) -> list[int]:
    """In ``send_outbound``'s transaction, after ``row`` became ``sent``."""
    if not _is_group_order_message(settings, row):
        return []
    is_post = (
        row.template_key == catalog.GROUP_ORDER_POST
        and row.message_type == OutboundMessageType.TEXT
        and row.wa_message_id is not None
    )
    if is_post:
        # Before the order row (lock order of ya_voy.orders.parking): reactions to this id wait.
        assert row.wa_message_id is not None
        await parking.lock_target(session, row.wa_message_id)
    order = await session.scalar(select(Order).where(Order.id == row.order_id).with_for_update())
    assert order is not None
    at = await now(session)
    if row.message_type == OutboundMessageType.DELETE:
        if (
            row.target_wa_message_id == order.group_message_id
            and order.group_post_deleted_at is None
        ):
            order.group_post_deleted_at = at
            await add_event(
                session,
                order,
                OrderEventType.GROUP_POST_DELETED,
                ActorType.SYSTEM,
                from_status=order.status,
                to_status=order.status,
            )
        return []
    if row.message_type != OutboundMessageType.TEXT or row.wa_message_id is None:
        return []
    out = Outbox(session)
    if is_post and order.group_message_id is None:
        order.group_message_id = row.wa_message_id
        if order.status == S.PUBLISHED and order.unclaimed_alert_sent_at is None:
            # CU-03 4a: the unclaimed period starts when the post is really in the group.
            order.unclaimed_alert_at = at + dt.timedelta(minutes=settings.unclaimed_alert_minutes)
        await session.flush()
        # Reactions that arrived before this commit go first, in WhatsApp order (D-19).
        for result in await parking.apply_parked(session, settings, row.wa_message_id, order.id):
            out.extend(result.outbound_ids)
        await session.refresh(order)
    if order.status == S.CANCELLED:
        # Sent after the cancellation listed the group messages: delete it too.
        await enqueue_group_delete(out, settings, order, row.wa_message_id)
    return out.ids


async def on_group_message_failed(
    session: AsyncSession, settings: Settings, row: OutboundMessage
) -> None:
    """A group delete failed for good: recorded (CU-09 6a). TODO(stage-6b): fall back to
    ``group_order_cancelled_edit`` and then ``group_order_cancelled`` before alerting."""
    if not _is_group_order_message(settings, row) or row.message_type != OutboundMessageType.DELETE:
        return
    order = await session.scalar(select(Order).where(Order.id == row.order_id).with_for_update())
    assert order is not None
    await add_event(
        session,
        order,
        OrderEventType.GROUP_POST_DELETE_FAILED,
        ActorType.SYSTEM,
        from_status=order.status,
        to_status=order.status,
        data={"target_wa_message_id": row.target_wa_message_id, "error": row.last_error},
    )


async def alert_unclaimed_orders(session: AsyncSession, settings: Settings) -> list[int]:
    """T15 / CU-08: once per publication period (a release restarts it). Rows are taken with
    ``FOR UPDATE SKIP LOCKED``; a claim holding the row wins and the alert is not sent."""
    at = await now(session)
    orders = list(
        await session.scalars(
            select(Order)
            .where(
                Order.status == S.PUBLISHED,
                Order.unclaimed_alert_sent_at.is_(None),
                Order.unclaimed_alert_at < at,
            )
            .order_by(Order.unclaimed_alert_at)
            .with_for_update(skip_locked=True)
        )
    )
    ids: list[int] = []
    for order in orders:
        store = await session.get(Store, order.store_id)
        assert store is not None and order.unclaimed_alert_at is not None
        period = order.unclaimed_alert_at.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        order.unclaimed_alert_sent_at = at
        await add_event(
            session,
            order,
            OrderEventType.UNCLAIMED_ALERT_SENT,
            ActorType.SYSTEM,
            from_status=S.PUBLISHED,
            to_status=S.PUBLISHED,
        )
        ids += await enqueue_admin_alert(
            session,
            settings,
            template_key=catalog.ADMIN_ORDER_UNCLAIMED,
            idempotency_base=key(order.id, catalog.ADMIN_ORDER_UNCLAIMED, period),
            variables={
                "code": order.code,
                "minutes": settings.unclaimed_alert_minutes,
                "order_details": texts.details(order, store),
            },
        )
    return ids
