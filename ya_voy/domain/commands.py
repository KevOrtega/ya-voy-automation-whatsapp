"""Store commands recognized by code, never by the LLM (CU-01 5a, CU-03 step 1, D-05).

Only the recognition lives here; what each command does is in ``ya_voy.orders.store_commands``
(stage 5a/6a).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum


class StoreCommand(StrEnum):
    CONFIRM = "confirm"
    REJECT = "reject"
    CANCEL = "cancel"
    HELP = "help"


# CU-03: "sí (o si, dale, ok, confirmo, 👍)"; CU-01 5a: cancelar, ayuda, sí, no.
_CONFIRM = {"si", "dale", "ok", "confirmo", "👍"}
_REJECT = {"no"}
_HELP = {"ayuda"}
_CANCEL_RE = re.compile(r"^cancelar(\s+\S+)?$")


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    plain = plain.replace("\ufe0f", "")  # emoji variation selector
    plain = re.sub(r"[.!¡¿?,;]+", " ", plain)
    return re.sub(r"\s+", " ", plain).strip()


def parse_store_command(text: str | None) -> StoreCommand | None:
    """The command a whole message expresses, or ``None`` if it is order data."""
    if not text:
        return None
    normalized = _normalize(text)
    if normalized in _CONFIRM:
        return StoreCommand.CONFIRM
    if normalized in _REJECT:
        return StoreCommand.REJECT
    if normalized in _HELP:
        return StoreCommand.HELP
    if _CANCEL_RE.match(normalized):
        return StoreCommand.CANCEL
    return None


def store_cancel_code(text: str | None) -> str | None:
    """The code in ``cancelar YV-0042`` (upper case), or ``None`` for a bare ``cancelar``."""
    if not text:
        return None
    match = _CANCEL_RE.match(_normalize(text))
    if match is None or match.group(1) is None:
        return None
    return match.group(1).strip().upper()


class AdminCommand(StrEnum):
    CONFIRM = "confirm"
    REJECT = "reject"
    CANCEL = "cancel"
    RELEASE = "release"
    STATUS = "status"
    HELP = "help"


@dataclass(frozen=True)
class ParsedAdminCommand:
    command: AdminCommand
    code: str | None = None  # cancelar / liberar


# Doc 07: same confirmation words as the store, without 👍 (store only).
_ADMIN_CONFIRM = _CONFIRM - {"👍"}
_ADMIN_STATUS = {"estado", "horario"}
_ADMIN_CODE_RE = re.compile(r"^(?P<verb>cancelar|liberar)\s+(?P<code>\S+)$")


def parse_admin_command(text: str | None) -> ParsedAdminCommand | None:
    """An admin command (doc 07 "Comandos reconocidos"), or ``None``: then the text is treated
    as a possible schedule instruction (CU-14)."""
    if not text:
        return None
    normalized = _normalize(text)
    if normalized in _ADMIN_CONFIRM:
        return ParsedAdminCommand(AdminCommand.CONFIRM)
    if normalized in _REJECT:
        return ParsedAdminCommand(AdminCommand.REJECT)
    if normalized in _ADMIN_STATUS:
        return ParsedAdminCommand(AdminCommand.STATUS)
    if normalized in _HELP:
        return ParsedAdminCommand(AdminCommand.HELP)
    if match := _ADMIN_CODE_RE.match(normalized):
        verb = AdminCommand.CANCEL if match["verb"] == "cancelar" else AdminCommand.RELEASE
        return ParsedAdminCommand(verb, match["code"].upper())
    return None
