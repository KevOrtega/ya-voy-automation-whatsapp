"""Effects of the store commands recognized by code (CU-01 5a, CU-03, CU-09; docs/03 T06-T08,
T16, T17). The store is already identified (stage 4b classifies the sender).

Runs inside ``intake.handle_store_text``'s transaction, under the store's Redis lock; the order
row is always locked ``FOR UPDATE`` before deciding, so a concurrent claim, release or admin
command is serialized on the row.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ya_voy.config import Settings
from ya_voy.db.models import Order, OutboundMessage, Store
from ya_voy.domain.commands import StoreCommand, store_cancel_code
from ya_voy.domain.enums import IN_PREPARATION_STATUSES, CancelledBy, OrderStatus
from ya_voy.messaging import catalog
from ya_voy.messaging import order_texts as texts
from ya_voy.orders.cancellation import CANCELLABLE, cancel_locked
from ya_voy.orders.notify import OrderResult, Outbox, key
from ya_voy.orders.publication import publish

S = OrderStatus
ACTIVE = (S.PUBLISHED, S.ASSIGNED)


async def _lock(session: AsyncSession, order_id: int) -> Order:
    order = await session.scalar(select(Order).where(Order.id == order_id).with_for_update())
    assert order is not None
    return order


async def _lock_awaiting(session: AsyncSession, store_id: int) -> Order | None:
    return await session.scalar(
        select(Order)
        .where(Order.store_id == store_id, Order.status == S.AWAITING_CONFIRMATION)
        .with_for_update()
    )


async def _quoted_order_id(
    session: AsyncSession, store: Store, quoted_wa_message_id: str | None
) -> tuple[int | None, OutboundMessage | None]:
    """The order of a bot message the store quoted (summary, confirmation...)."""
    if not quoted_wa_message_id:
        return None, None
    row = await session.scalar(
        select(OutboundMessage)
        .where(
            OutboundMessage.wa_message_id == quoted_wa_message_id,
            OutboundMessage.recipient_jid == store.wa_jid,
            OutboundMessage.order_id.is_not(None),
        )
        .order_by(OutboundMessage.id.desc())
        .limit(1)
    )
    return (row.order_id, row) if row is not None else (None, None)


async def _active_codes(session: AsyncSession, store_id: int) -> list[str]:
    rows = await session.scalars(
        select(Order.code)
        .where(Order.store_id == store_id, Order.status.in_(ACTIVE))
        .order_by(Order.code)
    )
    return [code for code in rows if code]


async def handle_store_command(
    session: AsyncSession,
    settings: Settings,
    store: Store,
    command: StoreCommand,
    *,
    text: str,
    quoted_wa_message_id: str | None,
    request_key: str,
    inbound_event_id: int | None = None,
) -> OrderResult:
    if command == StoreCommand.CONFIRM:
        return await confirm(
            session,
            settings,
            store,
            quoted_wa_message_id=quoted_wa_message_id,
            request_key=request_key,
            inbound_event_id=inbound_event_id,
        )
    if command == StoreCommand.REJECT:
        return await reject(session, settings, store, inbound_event_id=inbound_event_id)
    if command == StoreCommand.CANCEL:
        return await cancel(
            session,
            settings,
            store,
            code=store_cancel_code(text),
            quoted_wa_message_id=quoted_wa_message_id,
            request_key=request_key,
            inbound_event_id=inbound_event_id,
        )
    # TODO(stage-4b): HELP -> store_help at most once per HELP_REPLY_COOLDOWN_MINUTES.
    return OrderResult("command")


async def confirm(
    session: AsyncSession,
    settings: Settings,
    store: Store,
    *,
    quoted_wa_message_id: str | None,
    request_key: str,
    inbound_event_id: int | None = None,
) -> OrderResult:
    """CU-03: ``sí`` publishes the order awaiting confirmation (T06) or, closed, T07.

    Without an order awaiting confirmation (none, or a draft still being evaluated) nothing
    happens. CU-03 1d: a ``sí`` quoting a summary whose text differs from the current one (a
    correction came after it) is not a confirmation; the current summary is sent again. A quoted
    summary with the current text (re-sent without changes) confirms.
    """
    order = await _lock_awaiting(session, store.id)
    if order is None:
        return OrderResult("ignored:nothing_to_confirm")
    _, quoted = await _quoted_order_id(session, store, quoted_wa_message_id)
    current = texts.confirm_request(order, store)
    if (
        quoted is not None
        and quoted.order_id == order.id
        and quoted.template_key == catalog.STORE_ORDER_CONFIRM_REQUEST
        and quoted.body != current
    ):
        out = Outbox(session)
        t = catalog.STORE_ORDER_CONFIRM_REQUEST
        await out.add(
            key(order.id, f"{t}_resend", request_key),
            t,
            store.wa_jid,
            current,
            order_id=order.id,
        )
        return OrderResult("summary_resent", order.id, out.ids)
    return await publish(
        session,
        settings,
        order,
        store,
        request_key=request_key,
        inbound_event_id=inbound_event_id,
    )


async def reject(
    session: AsyncSession, settings: Settings, store: Store, *, inbound_event_id: int | None = None
) -> OrderResult:
    """CU-03 1b: ``no`` discards the order awaiting confirmation (T08); otherwise nothing."""
    order = await _lock_awaiting(session, store.id)
    if order is None:
        return OrderResult("ignored:nothing_to_reject")
    return await cancel_locked(
        session,
        settings,
        order,
        actor=CancelledBy.STORE,
        actor_id=store.id,
        inbound_event_id=inbound_event_id,
    )


async def cancel(
    session: AsyncSession,
    settings: Settings,
    store: Store,
    *,
    code: str | None,
    quoted_wa_message_id: str | None,
    request_key: str,
    inbound_event_id: int | None = None,
) -> OrderResult:
    """CU-09: by quoted message, by code, or the only active / in-preparation order."""
    out = Outbox(session)
    quoted_id, _ = await _quoted_order_id(session, store, quoted_wa_message_id)
    target: int | None = quoted_id
    if target is None and code is not None:
        target = await session.scalar(
            select(Order.id).where(Order.code == code, Order.store_id == store.id)
        )
        if target is None:  # 2b: unknown code or another store's
            t = catalog.STORE_ORDER_NOT_FOUND
            codes = await _active_codes(session, store.id)
            await out.add(
                f"{t}:{request_key}",
                t,
                store.wa_jid,
                catalog.render(t, active_codes=texts.active_codes(codes)),
            )
            return OrderResult("not_found", None, out.ids)
    if target is None:
        candidates = list(
            await session.scalars(
                select(Order.id)
                .where(
                    Order.store_id == store.id,
                    Order.status.in_([*IN_PREPARATION_STATUSES, *ACTIVE]),
                )
                .order_by(Order.id)
            )
        )
        if not candidates:
            t = catalog.STORE_CANCEL_NOTHING_ACTIVE  # proposed in 5a/6a
            await out.add(f"{t}:{request_key}", t, store.wa_jid, catalog.render(t))
            return OrderResult("nothing_active", None, out.ids)
        if len(candidates) > 1:  # 2a
            codes = await _active_codes(session, store.id)
            t = catalog.STORE_CANCEL_AMBIGUOUS
            await out.add(
                f"{t}:{request_key}",
                t,
                store.wa_jid,
                catalog.render(
                    t,
                    active_codes=texts.active_codes(codes),
                    code=codes[0] if codes else "",
                ),
            )
            return OrderResult("ambiguous", None, out.ids)
        target = candidates[0]

    order = await _lock(session, target)
    if order.status not in CANCELLABLE:  # 3a: already cancelled or expired
        t = catalog.STORE_CANCEL_NOT_ALLOWED
        await out.add(
            f"{t}:{request_key}",
            t,
            store.wa_jid,
            catalog.render(t, code=order.code or "", status_label=texts.status_label(order.status)),
            order_id=order.id,
        )
        return OrderResult(f"not_allowed:{order.status}", order.id, out.ids)
    return await cancel_locked(
        session,
        settings,
        order,
        actor=CancelledBy.STORE,
        actor_id=store.id,
        inbound_event_id=inbound_event_id,
    )
