"""Structured outputs the LLM must return (docs/05-modelo-datos.md).

The LLM never decides: these are validated again in code (RF-13, D-05, D-10).
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel

from ya_voy.domain.enums import PaymentMethod, ScheduleOverrideKind

__all__ = ["OrderExtraction", "PaymentMethod", "ScheduleOverrideIntent", "ScheduleOverrideKind"]


class OrderExtraction(BaseModel):
    """Every field is optional: the LLM extracts what it finds and code decides what is missing."""

    pickup_address: str | None = None
    delivery_address: str | None = None
    product: str | None = None
    payment_method: PaymentMethod | None = None
    payment_method_raw: str | None = None  # original wording, e.g. "transferencia"
    notes: str | None = None


class ScheduleOverrideIntent(BaseModel):
    kind: ScheduleOverrideKind | None = None  # None = not a schedule instruction
    # closed_window / open_window; defaults to today's business date
    date: dt.date | None = None
    start_time: dt.time | None = None  # closed_window / open_window
    # closed_window / open_window; may be past midnight (e.g. 00:00, 01:00)
    end_time: dt.time | None = None
    reason: str | None = None  # short noun phrase usable after "por", e.g. "lluvia"
