"""Pure texts and parsing of stages 5a/6a (doc 07; proposals pending the client's approval)."""

from __future__ import annotations

import pytest

from ya_voy.db.models import Order, Store
from ya_voy.domain.commands import store_cancel_code
from ya_voy.messaging import catalog
from ya_voy.messaging import order_texts as texts


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("cancelar", None),
        ("Cancelar", None),
        ("cancelar YV-0042", "YV-0042"),
        ("cancelar yv-0042", "YV-0042"),
        ("  CANCELAR   yv-7 ", "YV-7"),
        ("hola", None),
        (None, None),
    ],
)
def test_store_cancel_code(text: str | None, code: str | None) -> None:
    assert store_cancel_code(text) == code


def test_active_codes() -> None:
    assert texts.active_codes(["YV-0001", "YV-0002"]) == "YV-0001, YV-0002"
    assert texts.active_codes([]) == "ninguno"  # proposed in 5a/6a


def test_courier_notes() -> None:
    assert texts.store_courier_note("Joel") == "\nYa le avisé a Joel."
    assert texts.store_courier_note(None) == ""
    assert texts.admin_courier_note("Joel") == " Le avisé a Joel."
    assert texts.admin_courier_note(None) == ""


def test_command_error_reasons() -> None:
    assert texts.command_error_reason("not_found") == "no existe un pedido con ese código"
    assert texts.command_error_reason("already", "cancelled") == "ya está cancelado"
    assert texts.command_error_reason("already", "expired") == "ya está vencido"
    assert texts.command_error_reason("not_assigned") == "no está asignado a ningún motero"


def test_order_details_are_the_same_block_everywhere() -> None:
    store = Store(display_name="Casa Nogal")
    order = Order(
        pickup_address="casa nogal almendros",
        delivery_address="el ceibo 2871",
        product="almacen",
        payment_method="cash",
        payment_method_raw="efectivo",
        notes="tocar timbre",
    )
    block = texts.details(order, store)
    assert block in catalog.render(catalog.GROUP_ORDER_POST, code="YV-1", order_details=block)
    assert texts.confirm_request(order, store).startswith("📝 Revisá tu pedido:\n\n" + block)


def test_store_cancel_nothing_active_is_a_proposal() -> None:
    assert catalog.render(catalog.STORE_CANCEL_NOTHING_ACTIVE) == (
        "No tenés pedidos activos para cancelar."
    )
