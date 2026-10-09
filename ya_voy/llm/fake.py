"""Deterministic offline ``OrderExtractor``: tests and keyless development (``LLM_PROVIDER=fake``).

It only understands the "label: value" layout of the canonical example (docs/01 §6) and its
variants; it is not a substitute for the real model. Free-form messages give an empty extraction.
"""

from __future__ import annotations

import re
import unicodedata

from ya_voy.llm.schemas import OrderExtraction

# Label keyword (normalized: lower case, no accents) -> field.
_LABELS: tuple[tuple[str, str], ...] = (
    ("retiro", "pickup_address"),
    ("entrega", "delivery_address"),
    ("producto", "product"),
    ("abona", "payment_method_raw"),
    ("pago", "payment_method_raw"),
    ("nota", "notes"),
    ("observacion", "notes"),
)
_LINE_RE = re.compile(r"^(?P<label>[^:]{1,60}):\s*(?P<value>.*)$")


def _plain(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


class FakeOrderExtractor:
    async def extract(self, text: str, current: OrderExtraction | None = None) -> OrderExtraction:
        found: dict[str, str] = {}
        for line in text.splitlines():
            match = _LINE_RE.match(line.strip())
            if not match or not match.group("value").strip():
                continue
            label = _plain(match.group("label"))
            for keyword, field in _LABELS:
                if keyword in label and field not in found:
                    found[field] = match.group("value").strip()
                    break
        # payment_method is left to the code dictionary (normalize_payment_method).
        return OrderExtraction(**found)
