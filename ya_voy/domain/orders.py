"""Order draft rules: merge extractions, validate required fields (pure code, no I/O).

RF-13/RF-14/RN-14, docs/01 §6, docs/05 ``OrderExtraction`` rules.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any

from ya_voy.domain.enums import PaymentMethod
from ya_voy.domain.payment import normalize_payment_method
from ya_voy.llm.schemas import OrderExtraction

# Required fields in the order they are asked for (docs/01 §6, RN-14). ``notes`` is optional.
REQUIRED_FIELDS: tuple[str, ...] = (
    "pickup_address",
    "delivery_address",
    "product",
    "payment_method",
)
TEXT_FIELDS: tuple[str, ...] = ("pickup_address", "delivery_address", "product", "notes")
MISSING = "missing"


@dataclass(frozen=True)
class ValidationIssue:
    field: str
    code: str = MISSING  # the only code in v1 (docs/05)

    def as_json(self) -> dict[str, str]:
        return {"field": self.field, "code": self.code}


@dataclass(frozen=True)
class OrderFields:
    """The data columns of an order in preparation."""

    pickup_address: str | None = None
    delivery_address: str | None = None
    product: str | None = None
    payment_method: PaymentMethod | None = None
    payment_method_raw: str | None = None
    notes: str | None = None

    @property
    def is_empty(self) -> bool:
        return all(value is None for value in asdict(self).values())

    def as_extraction(self) -> OrderExtraction:
        return OrderExtraction(**asdict(self))

    def as_columns(self) -> dict[str, Any]:
        values = asdict(self)
        if self.payment_method is not None:
            values["payment_method"] = str(self.payment_method)
        return values


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def clean_extraction(extraction: OrderExtraction) -> OrderFields:
    """Strip texts (empty -> ``None``) and re-normalize the payment method in code.

    If the LLM kept the original wording, the code dictionary decides the method; otherwise the
    LLM's method is kept as is.
    """
    raw = _clean(extraction.payment_method_raw)
    method = normalize_payment_method(raw) if raw else extraction.payment_method
    return OrderFields(
        pickup_address=_clean(extraction.pickup_address),
        delivery_address=_clean(extraction.delivery_address),
        product=_clean(extraction.product),
        payment_method=method,
        payment_method_raw=raw,
        notes=_clean(extraction.notes),
    )


def merge(current: OrderFields, extraction: OrderExtraction) -> OrderFields:
    """RF-14: new non-empty values replace the previous ones; empty values never erase."""
    new = clean_extraction(extraction)
    updates = {
        name: getattr(new, name)
        for name in (*TEXT_FIELDS, "payment_method")
        if getattr(new, name) is not None
    }
    if new.payment_method is not None:
        # The wording belongs to the method it came with (may be None if the LLM gave no raw).
        updates["payment_method_raw"] = new.payment_method_raw
    return replace(current, **updates)


def validate_required_fields(fields: OrderFields) -> list[ValidationIssue]:
    """RF-13: deterministic check; a field is missing if null or blank after stripping."""
    issues = []
    for name in REQUIRED_FIELDS:
        value = getattr(fields, name)
        if value is None or (isinstance(value, str) and not value.strip()):
            issues.append(ValidationIssue(name))
    return issues
