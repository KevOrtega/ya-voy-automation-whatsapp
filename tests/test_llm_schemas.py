"""OrderExtraction and ScheduleOverrideIntent accept the examples in docs/05."""

from __future__ import annotations

from datetime import date, time

import pytest
from pydantic import ValidationError

from ya_voy.llm.schemas import (
    OrderExtraction,
    PaymentMethod,
    ScheduleOverrideIntent,
    ScheduleOverrideKind,
)


def test_order_extraction_canonical_example() -> None:
    data = {
        "pickup_address": "casa nogal almendros",
        "delivery_address": "el ceibo 2871",
        "product": "almacen",
        "payment_method": "transfer",
        "payment_method_raw": "transferencia",
        "notes": None,
    }
    extraction = OrderExtraction.model_validate(data)
    assert extraction.payment_method is PaymentMethod.TRANSFER
    assert extraction.model_dump(mode="json") == data


def test_order_extraction_all_fields_optional() -> None:
    assert OrderExtraction().model_dump() == {
        "pickup_address": None,
        "delivery_address": None,
        "product": None,
        "payment_method": None,
        "payment_method_raw": None,
        "notes": None,
    }


def test_order_extraction_rejects_unknown_payment_method() -> None:
    with pytest.raises(ValidationError):
        OrderExtraction(payment_method="bitcoin")


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"kind": "closed_today", "reason": "lluvia"}, ScheduleOverrideKind.CLOSED_TODAY),
        (
            {"kind": "closed_until_further_notice", "reason": None},
            ScheduleOverrideKind.CLOSED_UNTIL_FURTHER_NOTICE,
        ),
        (
            {
                "kind": "closed_window",
                "date": "2026-10-09",
                "start_time": "15:00",
                "end_time": "18:00",
            },
            ScheduleOverrideKind.CLOSED_WINDOW,
        ),
        (
            {
                "kind": "open_window",
                "date": "2026-10-09",
                "start_time": "23:00",
                "end_time": "00:00",
            },
            ScheduleOverrideKind.OPEN_WINDOW,
        ),
        ({"kind": "reopen"}, ScheduleOverrideKind.REOPEN),
        ({"kind": None}, None),
    ],
)
def test_schedule_intent_doc_examples(payload: dict, expected: ScheduleOverrideKind | None) -> None:
    intent = ScheduleOverrideIntent.model_validate(payload)
    assert intent.kind == expected


def test_schedule_intent_parses_dates_and_midnight() -> None:
    intent = ScheduleOverrideIntent.model_validate(
        {"kind": "open_window", "date": "2026-10-09", "start_time": "23:00", "end_time": "00:00"}
    )
    assert intent.date == date(2026, 10, 9)
    assert intent.start_time == time(23, 0)
    assert intent.end_time == time(0, 0)


def test_schedule_intent_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        ScheduleOverrideIntent(kind="closed_forever")
