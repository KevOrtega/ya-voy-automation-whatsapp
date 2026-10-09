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
STORE_OUT_OF_HOURS = "store_out_of_hours"
STORE_CLOSED_TODAY = "store_closed_today"
STORE_CLOSED_UNTIL_FURTHER_NOTICE = "store_closed_until_further_notice"
STORE_CLOSED_WINDOW = "store_closed_window"
ADMIN_HELP = "admin_help"
ADMIN_SCHEDULE_CONFIRM = "admin_schedule_confirm"
ADMIN_SCHEDULE_APPLIED = "admin_schedule_applied"
ADMIN_SCHEDULE_REJECTED = "admin_schedule_rejected"
ADMIN_SCHEDULE_CONFIRMATION_EXPIRED = "admin_schedule_confirmation_expired"
ADMIN_SCHEDULE_INVALID = "admin_schedule_invalid"
ADMIN_SCHEDULE_PARSE_ERROR = "admin_schedule_parse_error"
ADMIN_SCHEDULE_NOTHING_TO_REOPEN = "admin_schedule_nothing_to_reopen"
ADMIN_SCHEDULE_STILL_CLOSED = "admin_schedule_still_closed"

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
    STORE_OUT_OF_HOURS: (
        "🌙 Estamos fuera de horario. Hoy tomamos pedidos de {effective_hours}. "
        "Mandalo de nuevo cuando abramos."
    ),
    STORE_CLOSED_TODAY: "🚫 Hoy no tomamos pedidos{reason_text}.",
    STORE_CLOSED_UNTIL_FURTHER_NOTICE: (
        "🚫 Por el momento no estamos tomando pedidos{reason_text}. Probá de nuevo más tarde."
    ),
    STORE_CLOSED_WINDOW: (
        "🚫 Hoy de {window_start} a {window_end} no tomamos pedidos{reason_text}. "
        "Escribinos de nuevo a partir de las {window_end}."
    ),
    # "{código}" is literal text in doc 07, hence the doubled braces.
    ADMIN_HELP: (
        "Esto es lo que puedo hacer:\n"
        "*cancelar {{código}}*: cancela el pedido y lo saca del grupo\n"
        "*liberar {{código}}*: lo saca del motero y lo vuelve a ofrecer\n"
        "*estado*: horario y pedidos de hoy\n\n"
        "También podés cambiar el horario escribiendo, por ejemplo:\n"
        "_hoy no tomamos pedidos por lluvia_\n"
        "_de 15 a 18 no tomamos pedidos_\n"
        "_hoy no tomamos pedidos hasta nuevo aviso_\n"
        "_hoy hasta las 00_\n"
        "_ya tomamos pedidos de nuevo_"
    ),
    ADMIN_SCHEDULE_CONFIRM: (
        "Entendí esto:\n{override_summary}\n\n"
        "¿Lo aplico? Respondé *sí* o *no* (tenés {minutes} minutos)."
    ),
    ADMIN_SCHEDULE_APPLIED: "✅ Listo. {override_summary}\n{status_line}",
    ADMIN_SCHEDULE_REJECTED: "Ok, no cambié nada.",
    ADMIN_SCHEDULE_CONFIRMATION_EXPIRED: (
        "La confirmación venció y no cambié nada. Si querés, mandame la instrucción de nuevo."
    ),
    ADMIN_SCHEDULE_INVALID: (
        "No pude aplicar eso: {reason}. Probá de nuevo, por ejemplo: "
        "_de 15 a 18 no tomamos pedidos_ o _hoy hasta las 00_."
    ),
    ADMIN_SCHEDULE_PARSE_ERROR: (
        "No pude interpretar el mensaje en este momento. Probá de nuevo en unos minutos."
    ),
    ADMIN_SCHEDULE_NOTHING_TO_REOPEN: (
        "Ya se están tomando pedidos; no hay ningún cierre en vigor.{future_closures_note}"
    ),
    ADMIN_SCHEDULE_STILL_CLOSED: (
        "🔒 Recordatorio: la toma de pedidos sigue cerrada hasta nuevo aviso{reason_text} "
        "(desde el {since}). Para reabrir, escribí _ya tomamos pedidos de nuevo_."
    ),
}

# Schedule summaries ({override_summary}, doc 07 "Resúmenes de horario"), by kind.
OVERRIDE_SUMMARIES: dict[str, str] = {
    "closed_today": "Hoy {date} no se toman más pedidos{reason_paren}.",
    "closed_until_further_notice": (
        "Desde ahora no se toman pedidos hasta que me avises{reason_paren}."
    ),
    "closed_window": (
        "El {date} de {window_start} a {window_end} no se toman pedidos{reason_paren}."
    ),
    "open_window": (
        "El {date} se toman pedidos también de {window_start} a {window_end}{reason_paren}. "
        "Horario de ese día: {effective_hours}. {report_note}"
    ),
    "reopen": "Vuelvo a tomar pedidos ahora. Se levanta: {closures_list}.{future_closures_note}",
}
# {report_note} (doc 07).
REPORT_NOTE_MOVED = "El reporte del día se genera a las {window_end}."
REPORT_NOTE_REOPENED = (
    "El reporte de ese día ya se envió: los pedidos nuevos irán en una corrección a las "
    "{window_end}."
)
REASON_PAREN = " (motivo: {reason})"
REASON_TEXT = " por {reason}"
FUTURE_CLOSURES_NOTE = " Sigue programado: {items}."
NEXT_DAY_SUFFIX = " (del {date})"
STATUS_LINE = "Ahora: {accepting_label}"
ACCEPTING_LABEL_OPEN = "tomando pedidos"
ACCEPTING_LABEL_CLOSED = "cerrado ({reason})"

# Proposed in stage 7a, pending the client's approval (doc 07 marks them as proposals).
ACCEPTING_LABEL_CLOSED_NO_REASON = "cerrado"
OUTSIDE_HOURS_REASON = "fuera de horario"
CLOSURE_ITEMS: dict[str, str] = {
    "closed_today": "cierre de hoy{reason_text}",
    "closed_until_further_notice": "cierre hasta nuevo aviso{reason_text}",
    "closed_window": "cierre de {window_start} a {window_end}{reason_text}",
}
NO_EFFECTIVE_HOURS = "cerrado todo el día"
# Appended to {override_summary} when the new instruction overrides earlier ones (most recent wins).
REPLACED_NOTE = "Reemplaza, en el horario que se superpone: {items}."
OPENING_ITEM = "apertura del {date} de {window_start} a {window_end}{reason_text}"
# {reason} of admin_schedule_invalid. The first three are doc 07's examples.
SCHEDULE_INVALID_REASONS: dict[str, str] = {
    "open_out_of_bounds": (
        "la apertura tiene que quedar entre las {earliest} y las {latest} del día siguiente"
    ),
    "open_inside_base": "ese horario ya está dentro del horario normal",
    "end_before_start": "la hora de fin tiene que ser posterior a la de inicio",
    "missing_times": "me faltan la hora de inicio y la de fin",
    "past_date": "esa fecha ya pasó",
    "already_over": "ese horario ya terminó",
    "closure_outside_hours": "en ese horario ya no se toman pedidos",
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
