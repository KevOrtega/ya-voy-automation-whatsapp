"""Templates from docs/07-catalogo-mensajes.md, keyed by ``template_key``.

Only the templates used so far are here; later stages add theirs. Texts are copied verbatim
from doc 07 (user-facing Spanish).
"""

from __future__ import annotations

import string
from typing import Any

ADMIN_RATE_LIMITED_SENDER = "admin_rate_limited_sender"
ADMIN_OUTBOUND_FAILED = "admin_outbound_failed"
ADMIN_LLM_FAILURE = "admin_llm_failure"
STORE_ORDER_MISSING_FIELDS = "store_order_missing_fields"
STORE_ORDER_FORMAT_EXAMPLE = "store_order_format_example"
STORE_ORDER_CONFIRM_REQUEST = "store_order_confirm_request"
STORE_HELP = "store_help"
STORE_UNSUPPORTED_MEDIA = "store_unsupported_media"
STORE_PROCESSING_ERROR = "store_processing_error"
STORE_DRAFT_EXPIRED = "store_draft_expired"

TEMPLATES: dict[str, str] = {
    ADMIN_RATE_LIMITED_SENDER: (
        "ℹ️ {sender_name} mandó demasiados mensajes seguidos; los estoy ignorando por un rato."
    ),
    ADMIN_OUTBOUND_FAILED: (
        "❗ No se pudo enviar el mensaje `{template_key}` a {recipient_name} tras {attempts_text}."
    ),
    ADMIN_LLM_FAILURE: (
        "❗ No pude procesar un mensaje de {store_name}: el servicio de lectura de pedidos no responde."
    ),
    STORE_ORDER_MISSING_FIELDS: (
        "Recibí tu pedido, pero me falta:\n{missing_list}\n\n"
        "Mandame esos datos en uno o varios mensajes. Para descartarlo, escribí *cancelar*."
    ),
    STORE_ORDER_FORMAT_EXAMPLE: (
        "Si te sirve, mandalo así:\n"
        "📍 Dirección de retiro: casa nogal almendros\n"
        "📍 Dirección de entrega: el ceibo 2871\n"
        "🥪 Producto: almacen\n"
        "💳 Abona: transferencia"
    ),
    STORE_ORDER_CONFIRM_REQUEST: (
        "📝 Revisá tu pedido:\n\n{order_details}\n\n"
        "¿Confirmás el pedido? Respondé *SÍ* para enviarlo o corregí lo que haga falta."
    ),
    STORE_HELP: (
        "Hola 👋 Para pedir un envío mandame:\n"
        "📍 Dirección de retiro\n"
        "📍 Dirección de entrega\n"
        "🥪 Producto\n"
        "💳 Cómo abona\n\n"
        "Te voy a mostrar un resumen para que lo confirmes antes de enviarlo.\n"
        "Para cancelar un pedido: respondé su mensaje con *cancelar* o escribí *cancelar* y el código."
    ),
    STORE_UNSUPPORTED_MEDIA: (
        "Por ahora solo puedo leer mensajes de texto. ¿Me lo mandás escrito?"
    ),
    STORE_DRAFT_EXPIRED: (
        "⌛ Descarté el pedido que estabas cargando porque pasaron {minutes} minutos sin respuesta; "
        "no se envió a los moteros. Si todavía lo necesitás, mandalo de nuevo completo."
    ),
    STORE_PROCESSING_ERROR: (
        "Tuve un problema para leer tu mensaje. Probá mandarlo de nuevo en unos minutos."
    ),
}

# Readable field names for {missing_list} (doc 07).
FIELD_LABELS: dict[str, str] = {
    "pickup_address": "Dirección de retiro",
    "delivery_address": "Dirección de entrega",
    "product": "Producto (qué hay que llevar)",
    "payment_method": "Cómo abona (efectivo, transferencia, ya pagado u otro)",
}

# {payment_method_label} (doc 07); ``other`` shows the original wording.
PAYMENT_METHOD_LABELS: dict[str, str] = {
    "cash": "efectivo",
    "transfer": "transferencia",
    "prepaid": "ya pagado",
}

# {recipient_name} for recipients that are neither a store nor a courier (doc 07).
COURIERS_GROUP_NAME = "el grupo de moteros"
ADMIN_RECIPIENT_NAME = "un administrador"
UNKNOWN_RECIPIENT_NAME = "un destinatario sin nombre"


def attempts_text(attempts: int) -> str:
    """``{attempts_text}`` (doc 07): ``"1 intento"``, otherwise ``"N intentos"``."""
    return f"{attempts} intento" if attempts == 1 else f"{attempts} intentos"


def template_fields(template_key: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(TEMPLATES[template_key]) if name}


def render(template_key: str, /, **variables: Any) -> str:
    """Render a template. Missing or unexpected variables raise ``KeyError``/``ValueError``."""
    expected = template_fields(template_key)
    missing = expected - variables.keys()
    if missing:
        raise KeyError(f"{template_key}: missing variables {sorted(missing)}")
    extra = variables.keys() - expected
    if extra:
        raise ValueError(f"{template_key}: unexpected variables {sorted(extra)}")
    return TEMPLATES[template_key].format(**variables)


def payment_method_label(payment_method: str | None, payment_method_raw: str | None) -> str:
    if payment_method in PAYMENT_METHOD_LABELS:
        return PAYMENT_METHOD_LABELS[payment_method]
    return payment_method_raw or ""


def order_details(
    *,
    store_name: str,
    pickup_address: str | None,
    delivery_address: str | None,
    product: str | None,
    payment_method: str | None,
    payment_method_raw: str | None,
    notes: str | None,
) -> str:
    """The ``{order_details}`` block (doc 07): same in the group, to the courier and the store.

    The notes line is left out when there are no notes.
    """
    lines = [
        f"🏪 *Local:* {store_name}",
        f"📍 *Retiro:* {pickup_address or ''}",
        f"📍 *Entrega:* {delivery_address or ''}",
        f"🥪 *Producto:* {product or ''}",
        f"💳 *Abona:* {payment_method_label(payment_method, payment_method_raw)}",
    ]
    if notes:
        lines.append(f"📝 *Notas:* {notes}")
    return "\n".join(lines)


def missing_list(fields: list[str]) -> str:
    """``{missing_list}``: one readable field name per line (doc 07)."""
    return "\n".join(FIELD_LABELS[field] for field in fields)
