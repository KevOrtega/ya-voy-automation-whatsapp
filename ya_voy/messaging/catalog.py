"""Templates from docs/07-catalogo-mensajes.md, keyed by ``template_key``.

Only the templates used so far are here; later stages add theirs. Texts are copied verbatim
from doc 07 (user-facing Spanish).
"""

from __future__ import annotations

import string
from typing import Any

ADMIN_RATE_LIMITED_SENDER = "admin_rate_limited_sender"
ADMIN_OUTBOUND_FAILED = "admin_outbound_failed"

TEMPLATES: dict[str, str] = {
    ADMIN_RATE_LIMITED_SENDER: (
        "ℹ️ {sender_name} mandó demasiados mensajes seguidos; los estoy ignorando por un rato."
    ),
    ADMIN_OUTBOUND_FAILED: (
        "❗ No se pudo enviar el mensaje `{template_key}` a {recipient_name} tras {attempts_text}."
    ),
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
