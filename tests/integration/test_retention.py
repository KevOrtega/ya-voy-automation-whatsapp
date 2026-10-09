"""Data retention against Postgres (RNF-10, DC-11, RNF-09; docs/06 "Retención de datos").

"Now" is frozen through ``schedule.repository.db_now`` at 2026-10-09 12:00 (Buenos Aires), so with
``DATA_RETENTION_DAYS=90`` the cutoff is 2026-07-11 12:00 and the business day 2026-07-11. Rows are
never deleted: the tests count every table before and after.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tests.integration import factories as f
from tests.schedule_helpers import at
from ya_voy.db import models as m
from ya_voy.db.base import Base
from ya_voy.reports import repository as report_repo
from ya_voy.retention import service
from ya_voy.schedule import repository as schedule_repository
from ya_voy.worker.retention import apply_data_retention

Ctx = Callable[..., dict[str, Any]]
SF = async_sessionmaker[AsyncSession]
NOW = at(9, 12)
OLD_DAY = dt.date(2026, 7, 1)  # past the window
RECENT_DAY = dt.date(2026, 7, 20)  # inside it
OLD = dt.datetime(2026, 7, 1, 15, 0, tzinfo=dt.UTC)
RECENT = dt.datetime(2026, 7, 20, 15, 0, tzinfo=dt.UTC)
EXTRACTION = {"pickup_address": "casa nogal almendros", "delivery_address": "el ceibo 2871"}
PERSONAL_ORDER = {
    "pickup_address": "casa nogal almendros",
    "delivery_address": "el ceibo 2871",
    "product": "almacen",
    "payment_method": "transfer",
    "payment_method_raw": "transferencia a Joel Pérez",
    "notes": "tocar timbre 2B, preguntar por Ana",
    "extraction": EXTRACTION,
    "validation_errors": [{"field": "payment_method"}],
}
JOEL_JID = "5491100000007@s.whatsapp.net"
STORE_JID = "5491155550000@s.whatsapp.net"
GROUP_JID = "120363000000000001@g.us"
ADMIN_JID = "5491100000001@s.whatsapp.net"  # ADMIN_PHONES in the worker_ctx fixture
SNAPSHOT = {"1": {"code": "YV-1", "status": "assigned", "courier_id": 7, "courier_name": "Joel"}}
CHANGES = [
    {"code": "YV-1", "type": "courier_changed", "from": "Joel", "to": "Pedro"},
    {"code": "YV-2", "type": "added", "from": None, "to": "Ana"},
]
EVENT_DATA = {"extraction": EXTRACTION, "admin_jid": "5491100000001", "error": "x", "emoji": "👍"}
CAPTURE_PAYLOAD = {"event": "messages.upsert", "data": {"pushName": "Joel", "text": "hola"}}


class Clock:
    def __init__(self, now: dt.datetime) -> None:
        self.now = now

    async def __call__(self, session: AsyncSession) -> dt.datetime:
        return self.now


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock(NOW)
    monkeypatch.setattr(schedule_repository, "db_now", c)
    return c


@pytest.fixture
def captures(tmp_path: Path) -> Path:
    return tmp_path / "captures"


@pytest.fixture
def ctx(worker_ctx: Ctx, captures: Path) -> dict[str, Any]:
    return worker_ctx(capture_dir=str(captures))


async def count_rows(engine: AsyncEngine) -> dict[str, int]:
    async with engine.connect() as conn:
        return {
            t.name: (await conn.execute(select(func.count()).select_from(t))).scalar_one()
            for t in Base.metadata.sorted_tables
        }


async def get(sf: SF, model: Any, row_id: int) -> Any:
    async with sf() as session:
        row = await session.get(model, row_id)
        assert row is not None
        return row


class Data:
    """One row per category, old and recent, plus the rows they hang from."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.ids: dict[str, int] = {}

    async def add(self, name: str, stmt: Any) -> int:
        self.ids[name] = await f.insert_ok(self.engine, stmt)
        return self.ids[name]

    async def setup(self) -> Data:
        store = await self.add("store", f.store(display_name="Casa Nogal"))
        courier = await self.add(
            "courier",
            f.courier(
                display_name="Joel", phone="5491100000007", phone_jid="5491100000007@s.whatsapp.net"
            ),
        )
        for age, day, created in (("old", OLD_DAY, OLD), ("recent", RECENT_DAY, RECENT)):
            order = await self.add(
                f"{age}_order",
                f.order(
                    store,
                    status="assigned",
                    business_date=day,
                    code=f"YV-{age}",
                    published_at=created,
                    assigned_at=created,
                    courier_id=courier,
                    last_courier_id=courier,
                    created_at=created,
                    **PERSONAL_ORDER,
                ),
            )
            await self.add(
                f"{age}_event",
                insert_event(
                    order,
                    created,
                    event_type="claimed",
                    courier_id=courier,
                    data={
                        "extraction": EXTRACTION,
                        "admin_jid": "5491100000001",
                        "error": "x",
                        "emoji": "👍",
                    },
                ),
            )
            inbound = await self.add(
                f"{age}_inbound",
                f.inbound(
                    status="processed",
                    received_at=created,
                    payload=CAPTURE_PAYLOAD,
                    remote_jid=GROUP_JID,
                    participant=JOEL_JID,
                    error="boom with text",
                ),
            )
            await self.add(
                f"{age}_message",
                f.message(
                    body="retiro casa nogal almendros, entrega el ceibo 2871",
                    created_at=created,
                    order_id=order,
                    store_id=store,
                    inbound_event_id=inbound,
                    chat_jid=STORE_JID,
                    sender_jid=STORE_JID,
                ),
            )
            await self.add(
                f"{age}_outbound",
                f.outbound(
                    body="✅ *Te asignamos el pedido* el ceibo 2871",
                    status="sent",
                    created_at=created,
                    order_id=order,
                    last_error="earlier failure",
                    wa_message_id=f"WA-{age}",
                    recipient_jid=STORE_JID,
                    idempotency_key=f"admin_rate_limited_sender:{day}:{STORE_JID}",
                ),
            )
            await self.add(
                f"{age}_override",
                f.override(
                    status="expired",
                    reason="lluvia en lo de Ana",
                    created_at=created,
                    business_date=day,
                ),
            )
            report = await self.add(
                f"{age}_report", f.daily_report(report_date=day, status="sent", due_at=created)
            )
            await self.add(
                f"{age}_revision",
                f.revision(
                    report,
                    status="sent",
                    file_name=f"reporte-ya-voy-{day}.xlsx",
                    file_content=b"PK xlsx with el ceibo 2871",
                    snapshot=SNAPSHOT,
                    changes=CHANGES,
                    created_at=created,
                ),
            )
        return self


def insert_event(order_id: int, created: dt.datetime, **kw: Any) -> Any:
    from sqlalchemy import insert

    values = {"order_id": order_id, "actor_type": "courier", "created_at": created} | kw
    return insert(m.OrderEvent).values(**values)


@pytest.fixture
async def data(engine: AsyncEngine) -> Data:
    return await Data(engine).setup()


async def run(ctx: dict[str, Any]) -> dict[str, int]:
    return await apply_data_retention(ctx)


# --- each category: past the window vs inside it ------------------------------------------------


async def test_personal_fields_past_the_window_are_redacted_and_rows_kept(
    ctx: dict[str, Any], data: Data, engine: AsyncEngine
) -> None:
    sf = ctx["session_factory"]
    before = await count_rows(engine)
    recent_before = {
        model: (await get(sf, model, data.ids[f"recent_{name}"])).__dict__.copy()
        for name, model in MODELS.items()
    }

    counts = await run(ctx)

    assert counts == {
        "orders": 1,
        "order_events": 1,
        "inbound_events": 1,
        "messages": 1,
        "outbound_messages": 1,
        "schedule_overrides": 1,
        "report_revisions": 1,
        "stores": 0,  # both still active (docs/06): see test_retention_identities.py
        "couriers": 0,
        "capture_files": 0,
    }
    assert await count_rows(engine) == before  # nothing deleted

    order = await get(sf, m.Order, data.ids["old_order"])
    assert (
        order.pickup_address,
        order.delivery_address,
        order.product,
        order.payment_method_raw,
        order.notes,
        order.extraction,
        order.validation_errors,
        order.anonymized_at,
    ) == (None, None, None, None, None, {}, [], NOW)
    # Kept: ids, code, statuses, times, FKs, the payment method category.
    assert (order.code, order.status, order.courier_id, order.last_courier_id) == (
        "YV-old",
        "assigned",
        data.ids["courier"],
        data.ids["courier"],
    )
    assert (order.store_id, order.payment_method, order.published_at, order.business_date) == (
        data.ids["store"],
        "transfer",
        OLD,
        OLD_DAY,
    )

    event = await get(sf, m.OrderEvent, data.ids["old_event"])
    assert (event.data, event.event_type, event.courier_id, event.anonymized_at) == (
        {"emoji": "👍"},
        "claimed",
        data.ids["courier"],
        NOW,
    )
    inbound = await get(sf, m.InboundEvent, data.ids["old_inbound"])
    assert (inbound.payload, inbound.error, inbound.status, inbound.anonymized_at) == (
        {},
        None,
        "processed",
        NOW,
    )
    assert (inbound.remote_jid, inbound.participant) == (GROUP_JID, None)  # the group stays
    assert inbound.wa_message_id  # the dedup key stays
    message = await get(sf, m.Message, data.ids["old_message"])
    assert (message.body, message.order_id, message.inbound_event_id, message.anonymized_at) == (
        None,
        data.ids["old_order"],
        data.ids["old_inbound"],
        NOW,
    )
    assert (message.chat_jid, message.sender_jid, message.store_id) == ("", None, data.ids["store"])
    outbound = await get(sf, m.OutboundMessage, data.ids["old_outbound"])
    assert (outbound.body, outbound.last_error, outbound.status, outbound.wa_message_id) == (
        "",
        None,
        "sent",
        "WA-old",
    )
    assert outbound.anonymized_at == NOW
    assert (outbound.recipient_jid, outbound.idempotency_key) == (
        "",
        f"anonymized:{outbound.id}",
    )
    override = await get(sf, m.ScheduleOverride, data.ids["old_override"])
    assert (override.source_text, override.parsed, override.reason, override.status) == (
        "",
        {},
        None,
        "expired",
    )
    assert override.created_by == ""  # "5491100000000" is not in ADMIN_PHONES
    revision = await get(sf, m.ReportRevision, data.ids["old_revision"])
    assert (revision.file_content, revision.anonymized_at) == (None, NOW)
    # Courier names become the fallback name; codes, statuses and courier ids stay.
    assert revision.snapshot == {"1": SNAPSHOT["1"] | {"courier_name": "Motero"}}
    assert revision.changes == [
        {"code": "YV-1", "type": "courier_changed", "from": "Motero", "to": "Motero"},
        {"code": "YV-2", "type": "added", "from": None, "to": "Motero"},
    ]
    assert revision.file_name == f"reporte-ya-voy-{OLD_DAY}.xlsx"

    # Inside the window: untouched, column by column.
    for name, model in MODELS.items():
        after = (await get(sf, model, data.ids[f"recent_{name}"])).__dict__
        before_row = recent_before[model]
        assert {k: v for k, v in after.items() if not k.startswith("_")} == {
            k: v for k, v in before_row.items() if not k.startswith("_")
        }, name


MODELS = {
    "order": m.Order,
    "event": m.OrderEvent,
    "inbound": m.InboundEvent,
    "message": m.Message,
    "outbound": m.OutboundMessage,
    "override": m.ScheduleOverride,
    "revision": m.ReportRevision,
}


async def test_the_old_report_still_adds_up(ctx: dict[str, Any], data: Data) -> None:
    async with ctx["session_factory"]() as session:
        orders_before = await report_repo.load_order_rows(session, OLD_DAY)
        couriers_before = await report_repo.load_courier_rows(session, OLD_DAY)
    await run(ctx)
    async with ctx["session_factory"]() as session:
        orders_after = await report_repo.load_order_rows(session, OLD_DAY)
        couriers_after = await report_repo.load_courier_rows(session, OLD_DAY)
    assert couriers_after == couriers_before
    assert [(o.order_id, o.code, o.status, o.courier_id) for o in orders_after] == [
        (o.order_id, o.code, o.status, o.courier_id) for o in orders_before
    ]
    assert [(o.pickup_address, o.delivery_address, o.product) for o in orders_after] == [
        (None, None, None)
    ]


async def test_a_second_run_changes_nothing(ctx: dict[str, Any], data: Data, clock: Clock) -> None:
    await run(ctx)
    clock.now = NOW + dt.timedelta(hours=1)
    counts = await run(ctx)
    assert set(counts.values()) == {0}
    order = await get(ctx["session_factory"], m.Order, data.ids["old_order"])
    assert order.anonymized_at == NOW  # not re-stamped


async def test_the_window_moves_with_the_clock(
    ctx: dict[str, Any], data: Data, clock: Clock
) -> None:
    await run(ctx)
    clock.now = at(20, 12)  # 2026-10-20: 2026-07-20 is now past the 90 days
    counts = await run(ctx)
    assert counts["orders"] == 1 and counts["messages"] == 1
    order = await get(ctx["session_factory"], m.Order, data.ids["recent_order"])
    assert order.delivery_address is None


async def test_the_window_is_configurable(worker_ctx: Ctx, data: Data, captures: Path) -> None:
    ctx = worker_ctx(data_retention_days=100, capture_dir=str(captures))
    assert set((await run(ctx)).values()) == {0}  # 2026-07-01 is only 100 days old


# --- live flows --------------------------------------------------------------------------------


async def test_live_rows_are_never_touched(ctx: dict[str, Any], engine: AsyncEngine) -> None:
    store = await f.insert_ok(engine, f.store())
    other_store = await f.insert_ok(engine, f.store())
    third_store = await f.insert_ok(engine, f.store())
    courier = await f.insert_ok(engine, f.courier())
    live: dict[str, tuple[Any, int]] = {}

    async def keep(name: str, model: Any, stmt: Any) -> int:
        row_id = await f.insert_ok(engine, stmt)
        live[name] = (model, row_id)
        return row_id

    # Old orders still pending (an open draft, one awaiting confirmation, one published).
    draft = await keep(
        "draft",
        m.Order,
        f.order(store, status="draft", business_date=OLD_DAY, created_at=OLD, **PERSONAL_ORDER),
    )
    await keep(
        "awaiting",
        m.Order,
        f.order(
            other_store,
            status="awaiting_confirmation",
            business_date=OLD_DAY,
            created_at=OLD,
            **PERSONAL_ORDER,
        ),
    )
    await keep(
        "published",
        m.Order,
        f.published(third_store, business_date=OLD_DAY, created_at=OLD, **PERSONAL_ORDER),
    )
    # A final order of a day whose report is still being corrected.
    day = dt.date(2026, 6, 30)
    await f.insert_ok(engine, f.daily_report(report_date=day, status="correction_pending"))
    await keep(
        "correcting",
        m.Order,
        f.published(
            store,
            status="assigned",
            courier_id=courier,
            business_date=day,
            created_at=OLD,
            **PERSONAL_ORDER,
        ),
    )
    # Messages and events of the open draft.
    await keep(
        "draft_message",
        m.Message,
        f.message(body="entrega el ceibo 2871", created_at=OLD, order_id=draft),
    )
    await keep(
        "draft_event",
        m.OrderEvent,
        insert_event(draft, OLD, event_type="created", data={"extraction": EXTRACTION}),
    )
    # Unsent outbox rows and unfinished inbound events.
    for status in ("pending", "sending"):
        await keep(
            f"outbound_{status}",
            m.OutboundMessage,
            f.outbound(body="texto", status=status, created_at=OLD),
        )
    for status in ("received", "processing"):
        await keep(
            f"inbound_{status}",
            m.InboundEvent,
            f.inbound(status=status, received_at=OLD, payload=CAPTURE_PAYLOAD),
        )
    # Schedule overrides pending or still in force.
    await keep(
        "override_pending",
        m.ScheduleOverride,
        f.override(created_at=OLD, confirmation_expires_at=OLD),
    )
    await keep(
        "override_until_further_notice",
        m.ScheduleOverride,
        f.override(
            kind="closed_until_further_notice",
            status="active",
            starts_at=OLD,
            confirmed_at=OLD,
            created_at=OLD,
            business_date=None,
            parsed={"kind": "closed_until_further_notice"},
        ),
    )
    # Report files: a report not sent yet, and a sent one whose document is still queued.
    for report_day, status in ((dt.date(2026, 6, 28), "waiting"), (dt.date(2026, 6, 29), "sent")):
        report = await f.insert_ok(engine, f.daily_report(report_date=report_day, status=status))
        revision = await keep(
            f"revision_{status}",
            m.ReportRevision,
            f.revision(report, status="generated", file_content=b"xlsx", created_at=OLD),
        )
        if status == "sent":
            await f.insert_ok(
                engine,
                f.outbound(
                    template_key="admin_daily_report",
                    message_type="document",
                    report_revision_id=revision,
                    status="pending",
                    created_at=RECENT,
                ),
            )

    before = {}
    for name, (model, row_id) in live.items():
        before[name] = {
            k: v
            for k, v in (await get(ctx["session_factory"], model, row_id)).__dict__.items()
            if not k.startswith("_")
        }
    counts = await run(ctx)
    assert set(counts.values()) == {0}, counts
    for name, (model, row_id) in live.items():
        after = {
            k: v
            for k, v in (await get(ctx["session_factory"], model, row_id)).__dict__.items()
            if not k.startswith("_")
        }
        assert after == before[name], name


async def test_rows_locked_by_a_live_flow_are_left_for_the_next_run(
    ctx: dict[str, Any], data: Data
) -> None:
    sf = ctx["session_factory"]
    async with sf() as session, session.begin():
        await session.execute(
            text("SELECT id FROM orders WHERE id = :id FOR UPDATE"), {"id": data.ids["old_order"]}
        )
        # A claim, cancel or report holds the row: retention does not wait for it.
        counts = await asyncio.wait_for(run(ctx), 10)
    assert counts["orders"] == 0 and counts["messages"] == 1
    assert (await get(sf, m.Order, data.ids["old_order"])).anonymized_at is None
    assert (await run(ctx))["orders"] == 1


# --- batches and concurrency ---------------------------------------------------------------


async def old_messages(engine: AsyncEngine, n: int) -> None:
    async with engine.begin() as conn:
        for _ in range(n):
            await conn.execute(f.message(body="el ceibo 2871", created_at=OLD))


@pytest.mark.parametrize("n", [3, 4, 5])
async def test_batch_boundaries(
    worker_ctx: Ctx, engine: AsyncEngine, captures: Path, n: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Batches of 2: an exact multiple ends with an empty batch, a remainder with a short one."""
    ctx = worker_ctx(data_retention_batch_size=2, capture_dir=str(captures))
    await old_messages(engine, n)
    sizes: list[int] = []
    original = service.anonymize_batch

    async def spy(session: AsyncSession, category: Any, params: dict[str, Any]) -> int:
        done = await original(session, category, params)
        if category.name == "messages":
            sizes.append(done)
        return done

    monkeypatch.setattr(service, "anonymize_batch", spy)
    assert (await run(ctx))["messages"] == n
    assert sizes == {3: [2, 1], 4: [2, 2, 0], 5: [2, 2, 1]}[n]
    async with engine.connect() as conn:
        left = await conn.scalar(
            select(func.count()).select_from(m.Message).where(m.Message.body.is_not(None))
        )
    assert left == 0


@pytest.mark.parametrize("attempt", range(3))
async def test_concurrent_runs_anonymize_each_row_once(
    worker_ctx: Ctx, engine: AsyncEngine, data: Data, captures: Path, attempt: int
) -> None:
    ctx = worker_ctx(data_retention_batch_size=3, capture_dir=str(captures))
    await old_messages(engine, 40)
    before = await count_rows(engine)
    results = await asyncio.gather(*(run(ctx) for _ in range(4)))
    assert sum(r["messages"] for r in results) == 41
    for name in (
        "orders",
        "order_events",
        "inbound_events",
        "outbound_messages",
        "report_revisions",
    ):
        assert sum(r[name] for r in results) == 1, name
    assert await count_rows(engine) == before
    async with engine.connect() as conn:
        assert (
            await conn.scalar(
                select(func.count()).select_from(m.Message).where(m.Message.anonymized_at.is_(None))
            )
            == 1
        )  # the recent one


# --- capture files -----------------------------------------------------------------------------


def write(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text("{}\n", encoding="utf-8")
    return path


async def test_old_capture_files_are_deleted(ctx: dict[str, Any], captures: Path) -> None:
    old = write(captures, "20260701T150000123456Z_messages.upsert_abcdef0123.json")
    recent = write(captures, "20260720T150000123456Z_messages.upsert_abcdef0123.json")
    mapping = write(captures, "pseudonyms.json")
    other = write(captures, "notes.txt")

    counts = await run(ctx)

    assert counts["capture_files"] == 1
    assert not old.exists()
    assert recent.exists() and mapping.exists() and other.exists()


async def test_missing_capture_folder_and_vanished_files_are_fine(
    ctx: dict[str, Any], captures: Path
) -> None:
    assert (await run(ctx))["capture_files"] == 0  # the folder does not exist
    old = write(captures, "20260701T150000123456Z_messages.upsert_abcdef0123.json")
    original = Path.unlink

    def vanish(self: Path, missing_ok: bool = False) -> None:
        original(self)
        original(self)  # as if another run removed it first: FileNotFoundError

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Path, "unlink", vanish)
        assert service.delete_old_captures(captures, NOW) == 0
    assert not old.exists()


# --- logging -----------------------------------------------------------------------------------


async def test_summary_at_info_and_failures_at_error(
    ctx: dict[str, Any],
    data: Data,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken = service.Category("broken", "orders", "no_such_column = 1", "notes = NULL")
    monkeypatch.setattr(service, "CATEGORIES", (broken, *service.CATEGORIES))
    caplog.set_level(logging.INFO, logger=service.__name__)

    counts = await run(ctx)

    assert "broken" not in counts and counts["orders"] == 1  # the others still ran
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert [(r.getMessage(), r.category) for r in errors] == [("data retention failed", "broken")]
    summary = [r for r in caplog.records if r.getMessage() == "data retention applied"]
    assert len(summary) == 1 and summary[0].levelno == logging.INFO
    assert summary[0].errors == ["broken"]
    assert summary[0].anonymized_orders == 1
