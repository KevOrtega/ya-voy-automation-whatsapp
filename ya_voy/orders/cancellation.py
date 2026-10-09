"""Cancellations (CU-09, CU-13 ``cancelar``; docs/03 T08, T16, T17).

The order row is locked first (``SELECT ... FOR UPDATE``) to read its status and courier; then
the conditional ``UPDATE ... WHERE status = :status_read``. A concurrent claim either committed
before (the lock reads ``assigned``: T17, the courier is told) or runs after and finds
``cancelled`` (T19). Group deletes are queued for the post and every bot reply about the order
that already has a WhatsApp id; a group message still in the outbox is deleted as soon as it is
sent (``publication.on_group_message_sent``).

TODO(stage-6b): when a delete fails, edit the message (``group_order_cancelled_edit``) and, if that
fails too, reply quoting it (``group_order_cancelled``); today the failure is recorded
(``group_post_delete_failed``) and the generic ``admin_outbound_failed`` goes out.
"""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ya_voy.config import Settings
from ya_voy.db.models import Courier, Order, OutboundMessage, Store
from ya_voy.domain.enums import (
    IN_PREPARATION_STATUSES,
    ActorType,
    CancelledBy,
    OrderEventType,
    OrderStatus,
    OutboundMessageType,
    OutboundStatus,
)
from ya_voy.messaging import catalog
from ya_voy.messaging import order_texts as texts
from ya_voy.orders.claims import admin_command_error, lock_order_by_code
from ya_voy.orders.notify import OrderResult, Outbox, add_event, courier_jid, key, now
from ya_voy.reports.service import mark_report_for_correction

S = OrderStatus
CANCELLABLE = (*IN_PREPARATION_STATUSES, S.PUBLISHED, S.ASSIGNED)


async def group_message_ids(session: AsyncSession, settings: Settings, order: Order) -> list[str]:
    """The post and the bot's replies about ``order`` in the group that WhatsApp already has."""
    rows = await session.scalars(
        select(OutboundMessage.wa_message_id).where(
            OutboundMessage.order_id == order.id,
            OutboundMessage.recipient_jid == settings.couriers_group_jid,
            OutboundMessage.message_type == OutboundMessageType.TEXT,
            OutboundMessage.status == OutboundStatus.SENT,
            OutboundMessage.wa_message_id.is_not(None),
        )
    )
    ids = [order.group_message_id] if order.group_message_id else []
    return ids + sorted({wa for wa in rows if wa and wa not in ids})


async def enqueue_group_delete(
    out: Outbox, settings: Settings, order: Order, wa_message_id: str
) -> None:
    t = catalog.GROUP_ORDER_DELETE
    await out.add(
        f"order:{order.id}:delete:{wa_message_id}",
        t,
        settings.couriers_group_jid,
        order_id=order.id,
        target_wa_message_id=wa_message_id,
        message_type=OutboundMessageType.DELETE,
    )


async def cancel_locked(
    session: AsyncSession,
    settings: Settings,
    order: Order,
    *,
    actor: CancelledBy,
    actor_id: int | None = None,
    admin_jid: str | None = None,
    inbound_event_id: int | None = None,
) -> OrderResult:
    """T08 / T16 / T17 on an order row the caller locked ``FOR UPDATE`` and whose status is in
    ``CANCELLABLE``."""
    from_status = order.status
    assert from_status in CANCELLABLE
    previous = await session.get(Courier, order.courier_id) if order.courier_id else None
    at = await now(session)
    cancelled = await session.scalar(
        update(Order)
        .where(Order.id == order.id, Order.status == from_status)  # docs/03 :status_read
        .values(
            status=S.CANCELLED, cancelled_at=at, cancelled_by=actor, courier_id=None, updated_at=at
        )
        .returning(Order.id)
        .execution_options(synchronize_session=False)
    )
    assert cancelled is not None, "the row is locked FOR UPDATE with the status read"
    await session.refresh(order)
    store = await session.get(Store, order.store_id)
    assert store is not None
    actor_type = ActorType.ADMIN if actor == CancelledBy.ADMIN else ActorType.STORE
    ev = await add_event(
        session,
        order,
        OrderEventType.CANCELLED,
        actor_type,
        actor_id=actor_id,
        courier_id=previous.id if previous else None,
        from_status=from_status,
        to_status=S.CANCELLED,
        inbound_event_id=inbound_event_id,
        data={"admin_jid": admin_jid} if admin_jid else None,
    )
    out = Outbox(session)
    suffix = f"e{ev.id}"
    if from_status in IN_PREPARATION_STATUSES:  # T08: nothing reached the group
        t = catalog.STORE_DRAFT_CANCELLED
        await out.add(
            key(order.id, t, suffix), t, store.wa_jid, catalog.render(t), order_id=order.id
        )
        return OrderResult("draft_cancelled", order.id, out.ids)

    for wa_message_id in await group_message_ids(session, settings, order):
        await enqueue_group_delete(out, settings, order, wa_message_id)
    name = previous.display_name if previous else None
    if previous is not None:  # T17
        t = catalog.COURIER_ORDER_CANCELLED
        await out.add(
            key(order.id, t, suffix),
            t,
            courier_jid(previous),
            catalog.render(t, code=order.code, order_details=texts.details(order, store)),
            order_id=order.id,
        )
    if actor == CancelledBy.STORE:
        t = catalog.STORE_ORDER_CANCELLED
        await out.add(
            key(order.id, t, suffix),
            t,
            store.wa_jid,
            catalog.render(t, code=order.code, courier_note=texts.store_courier_note(name)),
            order_id=order.id,
        )
    else:
        assert admin_jid is not None
        t = catalog.ADMIN_COMMAND_OK
        await out.add(
            key(order.id, t, suffix),
            t,
            admin_jid,
            catalog.render(
                t,
                code=order.code,
                action_label=catalog.ACTION_LABELS["cancel"],
                courier_note=texts.admin_courier_note(name),
            ),
            order_id=order.id,
        )
        t = catalog.STORE_ORDER_CANCELLED_BY_ADMIN
        await out.add(
            key(order.id, t, suffix),
            t,
            store.wa_jid,
            catalog.render(t, code=order.code, courier_note=texts.store_courier_note(name)),
            order_id=order.id,
        )
    await mark_report_for_correction(session, order.business_date)  # CU-09 / CU-17
    return OrderResult("cancelled_assigned" if previous else "cancelled", order.id, out.ids)


async def cancel_by_admin(
    session: AsyncSession,
    settings: Settings,
    *,
    code: str,
    admin_jid: str,
    request_key: str,
    inbound_event_id: int | None = None,
) -> OrderResult:
    """CU-13 ``cancelar {código}`` for a sender already recognized as an admin (any store).

    TODO(stage-7b): call it from the admin's private text (identity by number or ``@lid``).
    """
    order = await lock_order_by_code(session, code)
    if order is None or order.status not in (S.PUBLISHED, S.ASSIGNED):
        out = Outbox(session)
        if order is None:
            reason, status = "not_found", None
        else:
            reason, status = "already", order.status
        await admin_command_error(
            out,
            action="cancel",
            code=code.upper(),
            reason=reason,
            status=status,
            admin_jid=admin_jid,
            request_key=request_key,
            order_id=order.id if order else None,
        )
        return OrderResult(f"error:{reason}", order.id if order else None, out.ids)
    return await cancel_locked(
        session,
        settings,
        order,
        actor=CancelledBy.ADMIN,
        admin_jid=admin_jid,
        inbound_event_id=inbound_event_id,
    )
