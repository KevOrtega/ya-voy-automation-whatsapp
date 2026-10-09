"""Pure data of a report version: rows, snapshot and net changes (docs/05, RN-21, RN-23).

The snapshot keeps the documented shape ``{order_id: {code, status, courier_id, courier_name}}``;
it is the base of the next correction. ``diff_snapshots`` follows the table in docs/05.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ya_voy.domain.enums import OrderStatus

Snapshot = dict[str, dict[str, Any]]


class ChangeType(StrEnum):
    ADDED = "added"
    COURIER_CHANGED = "courier_changed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class OrderRow:
    """One row of the ``Pedidos`` sheet: a published order of the day (``assigned`` or
    ``cancelled``). Times are UTC; the sheet shows them in ``TZ``."""

    order_id: int
    code: str
    status: str
    created_at: dt.datetime
    assigned_at: dt.datetime | None
    store_name: str
    pickup_address: str | None
    delivery_address: str | None
    product: str | None
    payment_label: str | None
    courier_id: int | None
    courier_name: str | None


@dataclass(frozen=True)
class CourierRow:
    """One row of the ``Resumen`` sheet (docs/05 "Mapeo al resumen del reporte")."""

    courier_id: int
    name: str
    phone: str | None
    taken: int
    released: int
    cancelled: int


@dataclass(frozen=True)
class Change:
    code: str
    type: ChangeType
    from_courier: str | None
    to_courier: str | None

    def as_json(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "type": str(self.type),
            "from": self.from_courier,
            "to": self.to_courier,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Change:
        return cls(data["code"], ChangeType(data["type"]), data.get("from"), data.get("to"))


@dataclass(frozen=True)
class ReportData:
    """Everything a version of the report shows, read in one transaction."""

    report_date: dt.date
    revision: int
    orders: list[OrderRow]
    couriers: list[CourierRow]
    changes: list[Change] = field(default_factory=list)
    schedule_changes: list[str] = field(default_factory=list)

    @property
    def is_correction(self) -> bool:
        return self.revision > 1

    @property
    def orders_count(self) -> int:
        return len(self.orders)

    @property
    def assigned_count(self) -> int:
        return sum(o.status == OrderStatus.ASSIGNED for o in self.orders)

    @property
    def cancelled_count(self) -> int:
        return sum(o.status == OrderStatus.CANCELLED for o in self.orders)

    @property
    def couriers_count(self) -> int:
        return len(self.couriers)

    def change_by_code(self) -> dict[str, Change]:
        return {c.code: c for c in self.changes}


def snapshot_of(orders: list[OrderRow]) -> Snapshot:
    return {
        str(o.order_id): {
            "code": o.code,
            "status": o.status,
            "courier_id": o.courier_id,
            "courier_name": o.courier_name,
        }
        for o in orders
    }


def diff_snapshots(previous: Snapshot, current: Snapshot) -> list[Change]:
    """Net changes since the last version sent, one per order, sorted by code (docs/05).

    The courier is compared by id, so a release and a new claim by the same courier is no
    change (RN-23), even if the name changed meanwhile.
    """
    changes: list[Change] = []
    for order_id, now in current.items():
        before = previous.get(order_id)
        if before is None:
            changes.append(Change(now["code"], ChangeType.ADDED, None, now["courier_name"]))
        elif before["status"] == OrderStatus.ASSIGNED and now["status"] == OrderStatus.CANCELLED:
            changes.append(Change(now["code"], ChangeType.CANCELLED, before["courier_name"], None))
        elif (
            before["status"] == OrderStatus.ASSIGNED
            and now["status"] == OrderStatus.ASSIGNED
            and before["courier_id"] != now["courier_id"]
        ):
            changes.append(
                Change(
                    now["code"],
                    ChangeType.COURIER_CHANGED,
                    before["courier_name"],
                    now["courier_name"],
                )
            )
    return sorted(changes, key=lambda c: c.code)


def file_name(report_date: dt.date, revision: int) -> str:
    """``reporte-ya-voy-2026-10-09.xlsx`` / ``reporte-ya-voy-2026-10-09-correccion-2.xlsx``."""
    base = f"reporte-ya-voy-{report_date.isoformat()}"
    return f"{base}.xlsx" if revision == 1 else f"{base}-correccion-{revision}.xlsx"
