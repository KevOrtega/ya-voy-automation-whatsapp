"""Builders for report tests."""

from __future__ import annotations

import datetime as dt

from ya_voy.reports.model import CourierRow, OrderRow

UTC = dt.UTC


def order_row(
    order_id: int,
    code: str,
    status: str = "cancelled",
    *,
    courier: tuple[int, str] | None = None,
    created_at: dt.datetime = dt.datetime(2026, 10, 9, 15, 0, tzinfo=UTC),
    assigned_at: dt.datetime | None = None,
    store_name: str = "Casa Nogal",
    pickup: str = "casa nogal almendros",
    delivery: str = "el ceibo 2871",
    product: str = "almacen",
    payment: str = "transferencia",
) -> OrderRow:
    courier_id, courier_name = courier or (None, None)
    if status == "assigned" and assigned_at is None:
        assigned_at = created_at + dt.timedelta(minutes=5)
    return OrderRow(
        order_id=order_id,
        code=code,
        status=status,
        created_at=created_at,
        assigned_at=assigned_at,
        store_name=store_name,
        pickup_address=pickup,
        delivery_address=delivery,
        product=product,
        payment_label=payment,
        courier_id=courier_id,
        courier_name=courier_name,
    )


def courier_row(
    courier_id: int,
    name: str,
    *,
    phone: str | None = None,
    taken: int = 0,
    released: int = 0,
    cancelled: int = 0,
) -> CourierRow:
    return CourierRow(courier_id, name, phone, taken, released, cancelled)
