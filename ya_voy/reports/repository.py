"""Database reads of the daily report (docs/05 "Mapeo al resumen del reporte", RN-21)."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ya_voy.config import Settings
from ya_voy.db.models import Courier, Order, OrderEvent, ScheduleOverride, Store
from ya_voy.domain.enums import (
    PENDING_STATUSES,
    OrderEventType,
    OrderStatus,
    ScheduleOverrideKind,
    ScheduleOverrideStatus,
)
from ya_voy.messaging import catalog
from ya_voy.messaging.schedule_texts import override_item
from ya_voy.reports.model import CourierRow, OrderRow
from ya_voy.schedule.hours import OverrideView, day_bounds, report_due_at

REPORTED_STATUSES = (OrderStatus.ASSIGNED, OrderStatus.CANCELLED)
RELEASE_EVENTS = (OrderEventType.RELEASED_BY_COURIER, OrderEventType.RELEASED_BY_ADMIN)


async def lock_day_orders(session: AsyncSession, day: dt.date) -> list[tuple[int, str | None, str]]:
    """``(id, code, status)`` of every order of ``day``, share-locked with ``NOWAIT``.

    A transition in flight on one of them (claim, release, cancel) holds its row lock, so this
    raises ``LockNotAvailable`` instead of reading a state about to change; the caller retries on
    the next run. Once this returns, those transitions wait for our commit, and then see the
    report already ``generating`` (see ``service.mark_report_for_correction``).
    """
    rows = await session.execute(
        select(Order.id, Order.code, Order.status)
        .where(Order.business_date == day)
        .order_by(Order.id)
        .with_for_update(read=True, nowait=True)
    )
    return [(r.id, r.code, r.status) for r in rows]


def pending_codes(orders: list[tuple[int, str | None, str]]) -> tuple[int, list[str]]:
    """Pending count (drafts included) and the codes to show (drafts have none; docs/05)."""
    pending = [(code, status) for _, code, status in orders if status in PENDING_STATUSES]
    return len(pending), sorted(code for code, _ in pending if code)


async def first_order_date(session: AsyncSession) -> dt.date | None:
    return await session.scalar(select(func.min(Order.business_date)))


async def load_order_rows(session: AsyncSession, day: dt.date) -> list[OrderRow]:
    """Published orders of ``day`` (RN-21): ``assigned`` or ``cancelled`` with a code."""
    rows = await session.execute(
        select(Order, Store.display_name, Courier.display_name)
        .join(Store, Store.id == Order.store_id)
        .outerjoin(Courier, Courier.id == Order.courier_id)
        .where(
            Order.business_date == day,
            Order.published_at.is_not(None),
            Order.status.in_(REPORTED_STATUSES),
        )
        .order_by(Order.id)
    )
    result = []
    for order, store_name, courier_name in rows:
        assert order.code is not None  # CHECK orders_code_when_published
        result.append(
            OrderRow(
                order_id=order.id,
                code=order.code,
                status=order.status,
                created_at=order.created_at,
                assigned_at=order.assigned_at,
                store_name=store_name,
                pickup_address=order.pickup_address,
                delivery_address=order.delivery_address,
                product=order.product,
                payment_label=catalog.payment_method_label(
                    order.payment_method, order.payment_method_raw
                )
                or None,
                courier_id=order.courier_id,
                courier_name=courier_name,
            )
        )
    return result


async def load_courier_rows(session: AsyncSession, day: dt.date) -> list[CourierRow]:
    """``Resumen``: couriers with any non-zero count for the orders of ``day``."""
    taken = dict(
        (
            await session.execute(
                select(Order.courier_id, func.count())
                .where(Order.business_date == day, Order.status == OrderStatus.ASSIGNED)
                .group_by(Order.courier_id)
            )
        ).all()
    )

    async def events(*conditions: object) -> dict[int, int]:
        rows = await session.execute(
            select(OrderEvent.courier_id, func.count())
            .join(Order, Order.id == OrderEvent.order_id)
            .where(Order.business_date == day, OrderEvent.courier_id.is_not(None), *conditions)
            .group_by(OrderEvent.courier_id)
        )
        return dict(rows.all())

    released = await events(OrderEvent.event_type.in_(RELEASE_EVENTS))
    cancelled = await events(
        OrderEvent.event_type == OrderEventType.CANCELLED,
        OrderEvent.from_status == OrderStatus.ASSIGNED,
    )
    ids = set(taken) | set(released) | set(cancelled)
    if not ids:
        return []
    couriers = (await session.scalars(select(Courier).where(Courier.id.in_(ids)))).all()
    return [
        CourierRow(
            courier_id=c.id,
            name=c.display_name,
            phone=c.phone,  # None when only the @lid is known (CU-16)
            taken=taken.get(c.id, 0),
            released=released.get(c.id, 0),
            cancelled=cancelled.get(c.id, 0),
        )
        for c in couriers
    ]


async def openings_of_day(session: AsyncSession, day: dt.date) -> list[OverrideView]:
    rows = await session.scalars(
        select(ScheduleOverride).where(
            ScheduleOverride.kind == ScheduleOverrideKind.OPEN_WINDOW,
            ScheduleOverride.status == ScheduleOverrideStatus.ACTIVE,
            ScheduleOverride.business_date == day,
        )
    )
    return [OverrideView.of(r) for r in rows]


async def due_at(session: AsyncSession, settings: Settings, day: dt.date) -> dt.datetime:
    """``report_due_at(D)`` of the 7a schedule core (base end or the latest opening's end)."""
    return report_due_at(settings, await openings_of_day(session, day), day)


async def schedule_change_items(
    session: AsyncSession, settings: Settings, day: dt.date
) -> list[str]:
    """``{schedule_note}`` items: closures and openings that were in effect during ``day``
    (lifted closures count up to their ``cancelled_at``)."""
    bounds = day_bounds(settings, day)
    end = func.least(
        func.coalesce(ScheduleOverride.ends_at, bounds.end),
        func.coalesce(ScheduleOverride.cancelled_at, bounds.end),
    )
    rows = await session.scalars(
        select(ScheduleOverride)
        .where(
            ScheduleOverride.kind != ScheduleOverrideKind.REOPEN,
            ScheduleOverride.confirmed_at.is_not(None),
            ScheduleOverride.status.in_(
                [ScheduleOverrideStatus.ACTIVE, ScheduleOverrideStatus.CANCELLED]
            ),
            ScheduleOverride.starts_at < bounds.end,
            and_(end > bounds.start, end > ScheduleOverride.starts_at),
            or_(
                ScheduleOverride.business_date.is_(None),
                ScheduleOverride.business_date == day,
            ),
        )
        .order_by(ScheduleOverride.starts_at, ScheduleOverride.id)
    )
    return [override_item(OverrideView.of(r), settings) for r in rows]
