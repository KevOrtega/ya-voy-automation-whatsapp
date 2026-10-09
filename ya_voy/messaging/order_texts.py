"""Order message texts from doc 07 (pure functions; the core in ``ya_voy.orders`` sends them)."""

from __future__ import annotations

from collections.abc import Sequence

from ya_voy.config import Settings
from ya_voy.db.models import Order, Store
from ya_voy.messaging import catalog
from ya_voy.messaging.schedule_texts import effective_hours, reason_text
from ya_voy.schedule.hours import AcceptanceDecision, Interval


def details(order: Order, store: Store) -> str:
    """``{order_details}``: the same block in the group, to the courier and to the store."""
    return catalog.order_details(
        store_name=store.display_name,
        pickup_address=order.pickup_address,
        delivery_address=order.delivery_address,
        product=order.product,
        payment_method=order.payment_method,
        payment_method_raw=order.payment_method_raw,
        notes=order.notes,
    )


def confirm_request(order: Order, store: Store) -> str:
    """``store_order_confirm_request`` with the order's current data."""
    return catalog.render(catalog.STORE_ORDER_CONFIRM_REQUEST, order_details=details(order, store))


def active_codes(codes: Sequence[str]) -> str:
    """``{active_codes}``; ", " between codes and "ninguno" when empty (proposed in 5a/6a)."""
    return catalog.ACTIVE_CODES_SEPARATOR.join(codes) if codes else catalog.NO_ACTIVE_CODES


def store_courier_note(courier_name: str | None) -> str:
    """``{courier_note}`` of ``store_order_cancelled`` / ``store_order_cancelled_by_admin``."""
    return catalog.STORE_COURIER_NOTE.format(courier_name=courier_name) if courier_name else ""


def admin_courier_note(courier_name: str | None) -> str:
    """``{courier_note}`` of ``admin_command_ok``: only after cancelling an assigned order."""
    return catalog.ADMIN_COURIER_NOTE.format(courier_name=courier_name) if courier_name else ""


def status_label(status: str) -> str:
    return catalog.STATUS_LABELS[status]


def command_error_reason(reason: str, status: str | None = None) -> str:
    template = catalog.COMMAND_ERROR_REASONS[reason]
    return template.format(status_label=status_label(status)) if status else template


def not_sent_reason_text(
    decision: AcceptanceDecision, settings: Settings, today_hours: Sequence[Interval]
) -> str:
    """``{reason_text}`` of ``store_order_not_sent_closed`` (doc 07): the active closure's reason
    (" por lluvia", or empty); outside the effective hours, " (horario de hoy: ...)"."""
    assert not decision.accepting
    if decision.closure is not None:
        return reason_text(decision.closure.reason)
    return catalog.NOT_SENT_OUTSIDE_HOURS.format(
        effective_hours=effective_hours(today_hours, settings)
    )
