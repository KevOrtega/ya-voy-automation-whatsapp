"""User-facing intake texts rendered from the doc 07 catalog."""

from __future__ import annotations

from ya_voy.messaging import catalog

CANONICAL_DETAILS = (
    "🏪 *Local:* Casa Nogal\n"
    "📍 *Retiro:* casa nogal almendros\n"
    "📍 *Entrega:* el ceibo 2871\n"
    "🥪 *Producto:* almacen\n"
    "💳 *Abona:* transferencia"
)


def _details(**overrides: object) -> str:
    values: dict = {
        "store_name": "Casa Nogal",
        "pickup_address": "casa nogal almendros",
        "delivery_address": "el ceibo 2871",
        "product": "almacen",
        "payment_method": "transfer",
        "payment_method_raw": "transferencia",
        "notes": None,
    } | overrides
    return catalog.order_details(**values)


def test_order_details_matches_the_doc_07_example() -> None:
    assert _details() == CANONICAL_DETAILS


def test_order_details_with_notes_and_other_payment() -> None:
    text = _details(payment_method="other", payment_method_raw="cheque", notes="tocar timbre")
    assert "💳 *Abona:* cheque" in text
    assert text.endswith("\n📝 *Notas:* tocar timbre")


def test_payment_labels() -> None:
    assert catalog.payment_method_label("cash", "efvo") == "efectivo"
    assert catalog.payment_method_label("prepaid", None) == "ya pagado"
    assert catalog.payment_method_label("other", "cheque") == "cheque"


def test_confirmation_summary_for_the_canonical_order() -> None:
    text = catalog.render(catalog.STORE_ORDER_CONFIRM_REQUEST, order_details=_details())
    assert text == (
        "📝 Revisá tu pedido:\n\n"
        f"{CANONICAL_DETAILS}\n\n"
        "¿Confirmás el pedido? Respondé *SÍ* para enviarlo o corregí lo que haga falta."
    )


def test_missing_fields_message() -> None:
    text = catalog.render(
        catalog.STORE_ORDER_MISSING_FIELDS,
        missing_list=catalog.missing_list(["product", "payment_method"]),
    )
    assert text == (
        "Recibí tu pedido, pero me falta:\n"
        "Producto (qué hay que llevar)\n"
        "Cómo abona (efectivo, transferencia, ya pagado u otro)\n\n"
        "Mandame esos datos en uno o varios mensajes. Para descartarlo, escribí *cancelar*."
    )


def test_no_template_leaves_placeholders() -> None:
    for key, template in catalog.TEMPLATES.items():
        sample = {name: "x" for name in catalog.template_fields(key)}
        rendered = catalog.render(key, **sample).replace("{código}", "")  # literal in admin_help
        assert "{" not in rendered, template
