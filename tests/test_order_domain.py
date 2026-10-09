"""Pure intake rules with Kevin's canonical order (docs/01 §6) as the main case."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from tests.llm_stubs import CANONICAL_EXTRACTION, CANONICAL_ORDER_TEXT, StubExtractor
from ya_voy.domain.commands import StoreCommand, parse_store_command
from ya_voy.domain.enums import PaymentMethod
from ya_voy.domain.orders import (
    OrderFields,
    ValidationIssue,
    clean_extraction,
    merge,
    validate_required_fields,
)
from ya_voy.domain.payment import normalize_payment_method
from ya_voy.llm.extractor import OrderExtractor
from ya_voy.llm.fake import FakeOrderExtractor
from ya_voy.llm.schemas import OrderExtraction
from ya_voy.schedule.calendar import business_date

TZ = ZoneInfo("America/Argentina/Buenos_Aires")


def test_canonical_fixture_is_verbatim_from_doc_01() -> None:
    assert CANONICAL_ORDER_TEXT == (
        "📍 Dirección de retiro: casa nogal almendros\n"
        "📍 Dirección de entrega: el ceibo 2871\n"
        "🥪 Producto/: almacen\n"
        "💳Abona: transferencia\n"
    )


async def test_canonical_order_through_the_stub_llm_is_complete() -> None:
    stub = StubExtractor()
    assert isinstance(stub, OrderExtractor)
    extraction = await stub.extract(CANONICAL_ORDER_TEXT)
    fields = merge(OrderFields(), extraction)
    assert fields == OrderFields(
        pickup_address="casa nogal almendros",
        delivery_address="el ceibo 2871",
        product="almacen",
        payment_method=PaymentMethod.TRANSFER,
        payment_method_raw="transferencia",
        notes=None,
    )
    assert validate_required_fields(fields) == []


async def test_fake_extractor_reads_the_canonical_layout() -> None:
    extraction = await FakeOrderExtractor().extract(CANONICAL_ORDER_TEXT)
    # The fake leaves payment_method to the code dictionary; after cleaning it matches docs/05.
    assert clean_extraction(extraction) == clean_extraction(CANONICAL_EXTRACTION)


async def test_fake_extractor_without_emojis_and_with_missing_payment() -> None:
    text = "Direccion de retiro: kiosco 12\nDireccion de entrega: av italia 300\nProducto: farmacia"
    fields = clean_extraction(await FakeOrderExtractor().extract(text))
    assert (fields.pickup_address, fields.delivery_address, fields.product) == (
        "kiosco 12",
        "av italia 300",
        "farmacia",
    )
    assert validate_required_fields(fields) == [ValidationIssue("payment_method")]


async def test_fake_extractor_gives_nothing_for_small_talk() -> None:
    assert clean_extraction(await FakeOrderExtractor().extract("hola, gracias!")).is_empty


@pytest.mark.parametrize(
    ("missing", "issues"),
    [
        ({}, []),
        ({"payment_method": None, "payment_method_raw": None}, ["payment_method"]),
        ({"pickup_address": "   "}, ["pickup_address"]),
        ({"delivery_address": ""}, ["delivery_address"]),
        ({"product": None, "pickup_address": None}, ["pickup_address", "product"]),
    ],
)
def test_validate_required_fields_rn14(missing: dict, issues: list[str]) -> None:
    fields = OrderFields(
        **(merge(OrderFields(), CANONICAL_EXTRACTION).__dict__ | missing)  # type: ignore[arg-type]
    )
    assert [i.field for i in validate_required_fields(fields)] == issues
    assert all(i.code == "missing" for i in validate_required_fields(fields))


def test_notes_are_optional() -> None:
    fields = merge(OrderFields(), CANONICAL_EXTRACTION)
    assert fields.notes is None and validate_required_fields(fields) == []


def test_issue_json_shape_matches_doc_05() -> None:
    assert ValidationIssue("payment_method").as_json() == {
        "field": "payment_method",
        "code": "missing",
    }


def test_merge_new_non_empty_values_replace_rf14() -> None:
    current = merge(OrderFields(), CANONICAL_EXTRACTION)
    corrected = merge(current, OrderExtraction(delivery_address="el ceibo 2900", product="  "))
    assert corrected.delivery_address == "el ceibo 2900"
    assert corrected.product == "almacen"  # blank never erases
    assert corrected.pickup_address == "casa nogal almendros"


def test_merge_payment_raw_follows_its_method() -> None:
    current = merge(OrderFields(), CANONICAL_EXTRACTION)
    paid = merge(current, OrderExtraction(payment_method_raw="efectivo"))
    assert (paid.payment_method, paid.payment_method_raw) == (PaymentMethod.CASH, "efectivo")
    # The code dictionary prevails over the LLM (docs/05).
    llm_wrong = merge(
        OrderFields(), OrderExtraction(payment_method="cash", payment_method_raw="mp")
    )
    assert llm_wrong.payment_method == PaymentMethod.TRANSFER


def test_merge_keeps_llm_method_without_raw() -> None:
    fields = merge(OrderFields(), OrderExtraction(payment_method="prepaid"))
    assert (fields.payment_method, fields.payment_method_raw) == (PaymentMethod.PREPAID, None)


@pytest.mark.parametrize(
    ("raw", "method"),
    [
        ("efectivo", PaymentMethod.CASH),
        ("Efectivo!", PaymentMethod.CASH),
        ("transferencia", PaymentMethod.TRANSFER),
        ("transf", PaymentMethod.TRANSFER),
        ("MP", PaymentMethod.TRANSFER),
        ("Mercado Pago", PaymentMethod.TRANSFER),
        ("pagado", PaymentMethod.PREPAID),
        ("ya abonó", PaymentMethod.PREPAID),
        ("cheque", PaymentMethod.OTHER),
        ("tarjeta", PaymentMethod.OTHER),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_normalize_payment_method(raw: str | None, method: PaymentMethod | None) -> None:
    assert normalize_payment_method(raw) == method


@pytest.mark.parametrize(
    ("text", "command"),
    [
        ("sí", StoreCommand.CONFIRM),
        ("Si", StoreCommand.CONFIRM),
        ("SÍ!", StoreCommand.CONFIRM),
        ("dale", StoreCommand.CONFIRM),
        ("ok", StoreCommand.CONFIRM),
        ("confirmo", StoreCommand.CONFIRM),
        ("👍", StoreCommand.CONFIRM),
        ("no", StoreCommand.REJECT),
        ("cancelar", StoreCommand.CANCEL),
        ("Cancelar YV-0042", StoreCommand.CANCEL),
        ("ayuda", StoreCommand.HELP),
        ("si, la entrega es el ceibo 2900", None),
        ("no tengo el producto todavía", None),
        (CANONICAL_ORDER_TEXT, None),
        ("", None),
        (None, None),
    ],
)
def test_parse_store_command(text: str | None, command: StoreCommand | None) -> None:
    assert parse_store_command(text) == command


@pytest.mark.parametrize(
    ("local", "expected"),
    [
        (dt.datetime(2026, 10, 9, 12, 0), dt.date(2026, 10, 9)),
        (dt.datetime(2026, 10, 10, 0, 30), dt.date(2026, 10, 9)),  # extension past midnight
        (dt.datetime(2026, 10, 10, 4, 59), dt.date(2026, 10, 9)),
        (dt.datetime(2026, 10, 10, 5, 0), dt.date(2026, 10, 10)),
    ],
)
def test_business_date_rf09(local: dt.datetime, expected: dt.date) -> None:
    now = local.replace(tzinfo=TZ).astimezone(dt.UTC)
    assert business_date(now, TZ, dt.time(5, 0)) == expected


def test_business_date_requires_aware_datetime() -> None:
    with pytest.raises(ValueError):
        business_date(dt.datetime(2026, 10, 9, 12), TZ, dt.time(5))
