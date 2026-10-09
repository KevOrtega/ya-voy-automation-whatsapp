"""Store commands recognized by code, never by the LLM (CU-01 5a, CU-03 step 1, D-05).

Only the recognition lives here; what each command does is stage 4b (confirm/publish) and
stage 5 (cancel by code or quote).
"""

from __future__ import annotations

import re
import unicodedata
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
