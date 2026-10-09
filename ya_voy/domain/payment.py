"""Payment method normalization (docs/05 "Reglas para el prompt y el código", docs/01 §6).

The code re-normalizes ``payment_method_raw`` with its own dictionary and prevails over the LLM.
"""

from __future__ import annotations

import re
import unicodedata

from ya_voy.domain.enums import PaymentMethod

# Matched as whole words/phrases on the normalized text (lower case, no accents).
_KEYWORDS: tuple[tuple[PaymentMethod, tuple[str, ...]], ...] = (
    (PaymentMethod.CASH, ("efectivo",)),
    (PaymentMethod.TRANSFER, ("transferencia", "transf", "mp", "mercado pago")),
    (PaymentMethod.PREPAID, ("pagado", "ya abono")),
)


def _normalize_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    without_accents = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", without_accents)).strip()


def normalize_payment_method(raw: str | None) -> PaymentMethod | None:
    """``None`` for empty text; a known keyword -> its method; any other text -> ``other``."""
    if raw is None or not raw.strip():
        return None
    text = f" {_normalize_text(raw)} "
    for method, keywords in _KEYWORDS:
        if any(f" {keyword} " in text for keyword in keywords):
            return method
    return PaymentMethod.OTHER
