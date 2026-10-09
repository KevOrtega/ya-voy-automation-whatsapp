"""Retention of stores and couriers with no activity in ``DATA_RETENTION_DAYS`` (RNF-10, RNF-09;
docs/06 "Retención de datos", "Identidades inactivas").

"Now" is frozen at 2026-10-09 12:00 (Buenos Aires): the cutoff is 2026-07-11 12:00. Identities
created on 2026-07-01 with nothing newer are inactive. ``ADMIN_PHONES`` is
``5491100000001,5491100000002`` (``worker_ctx``).
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tests.integration import factories as f
from tests.schedule_helpers import at
from ya_voy.db import models as m
from ya_voy.messaging.outbox import display_name_for_jid
from ya_voy.orders.events import ReactionAdded
from ya_voy.reports import repository as report_repo
from ya_voy.retention import service
from ya_voy.schedule import repository as schedule_repository
from ya_voy.worker import orders as order_jobs
from ya_voy.worker.retention import apply_data_retention

Ctx = Callable[..., dict[str, Any]]
SF = async_sessionmaker[AsyncSession]
NOW = at(9, 12)
TODAY = dt.date(2026, 10, 9)
OLD_DAY = dt.date(2026, 7, 1)
RECENT_DAY = dt.date(2026, 7, 20)
OLD = dt.datetime(2026, 7, 1, 15, 0, tzinfo=dt.UTC)
RECENT = dt.datetime(2026, 7, 20, 15, 0, tzinfo=dt.UTC)
GROUP_JID = "120363000000000001@g.us"
ADMIN_PHONE = "5491100000001"
ADMIN_JID = f"{ADMIN_PHONE}@s.whatsapp.net"
STORE_JID = "5491155550000@s.whatsapp.net"
STORE_LID = "111111111111111@lid"
JOEL_JID = "5491100000007@s.whatsapp.net"
JOEL_LID = "222222222222222@lid"
POST = "GROUPPOST1"


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
def ctx(worker_ctx: Ctx, tmp_path: Any) -> dict[str, Any]:
    return worker_ctx(capture_dir=str(tmp_path / "captures"))


async def run(ctx: dict[str, Any]) -> dict[str, int]:
    return await apply_data_retention(ctx)


async def get(sf: SF, model: Any, row_id: int) -> Any:
    async with sf() as session:
        row = await session.get(model, row_id)
        assert row is not None
        return row


async def count_rows(engine: AsyncEngine) -> dict[str, int]:
    async with engine.connect() as conn:
        return {
            t.name: (await conn.execute(select(func.count()).select_from(t))).scalar_one()
            for t in m.Base.metadata.sorted_tables
        }


def old_store(**kw: Any) -> Any:
    return f.store(**{"created_at": OLD, "updated_at": OLD, **kw})


def old_courier(**kw: Any) -> Any:
    return f.courier(**{"first_seen_at": OLD, "last_seen_at": OLD, "updated_at": OLD, **kw})


async def old_history(engine: AsyncEngine, store: int, courier: int) -> dict[str, int]:
    """A delivered order of 2026-07-01 (report sent): everything the first pass anonymizes."""
    report = await f.insert_ok(
        engine, f.daily_report(report_date=OLD_DAY, status="sent", due_at=OLD)
    )
    order = await f.insert_ok(
        engine,
        f.published(
            store,
            status="assigned",
            code="YV-0001",
            business_date=OLD_DAY,
            courier_id=courier,
            last_courier_id=courier,
            published_at=OLD,
            assigned_at=OLD,
            created_at=OLD,
            delivery_address="el ceibo 2871",
        ),
    )
    await f.insert_ok(
        engine,
        insert(m.OrderEvent).values(
            order_id=order,
            event_type="claimed",
            actor_type="courier",
            actor_id=courier,
            courier_id=courier,
            created_at=OLD,
        ),
    )
    inbound = await f.insert_ok(
        engine,
        f.inbound(status="processed", received_at=OLD, remote_jid=STORE_JID),
    )
    await f.insert_ok(
        engine,
        f.message(
            chat_jid=STORE_JID,
            sender_jid=STORE_JID,
            store_id=store,
            order_id=order,
            inbound_event_id=inbound,
            body="el ceibo 2871",
            created_at=OLD,
        ),
    )
    await f.insert_ok(
        engine,
        f.inbound(status="processed", received_at=OLD, remote_jid=GROUP_JID, participant=JOEL_LID),
    )
    await f.insert_ok(
        engine,
        f.outbound(recipient_jid=JOEL_JID, status="sent", order_id=order, created_at=OLD),
    )
    await f.insert_ok(
        engine,
        f.revision(
            report,
            status="sent",
            snapshot={
                str(order): {
                    "code": "YV-0001",
                    "status": "assigned",
                    "courier_id": courier,
                    "courier_name": "Joel",
                }
            },
            created_at=OLD,
        ),
    )
    return {"order": order, "report": report}


class Person:
    """A store and a courier active only on 2026-07-01."""

    async def setup(self, engine: AsyncEngine) -> Person:
        self.store = await f.insert_ok(
            engine,
            old_store(
                wa_jid=STORE_JID, phone="5491155550000", lid=STORE_LID, display_name="Casa Nogal"
            ),
        )
        self.courier = await f.insert_ok(
            engine,
            old_courier(
                lid=JOEL_LID, phone_jid=JOEL_JID, phone="5491100000007", display_name="Joel"
            ),
        )
        self.history = await old_history(engine, self.store, self.courier)
        return self


@pytest.fixture
async def person(engine: AsyncEngine) -> Person:
    return await Person().setup(engine)


def identity(row: Any) -> tuple[Any, ...]:
    if isinstance(row, m.Store):
        return (row.wa_jid, row.phone, row.lid, row.display_name)
    return (row.lid, row.phone_jid, row.phone, row.display_name)


# --- inactive vs active --------------------------------------------------------------------------


async def test_an_inactive_store_and_courier_lose_every_identifier(
    ctx: dict[str, Any], person: Person, engine: AsyncEngine
) -> None:
    sf = ctx["session_factory"]
    before = await count_rows(engine)

    counts = await run(ctx)

    assert (counts["stores"], counts["couriers"]) == (1, 1)
    assert await count_rows(engine) == before  # nothing deleted
    store = await get(sf, m.Store, person.store)
    assert identity(store) == (None, None, None, "Local")  # STORE_FALLBACK_NAME
    assert (store.anonymized_at, store.created_at, store.is_blocked) == (NOW, OLD, False)
    courier = await get(sf, m.Courier, person.courier)
    assert identity(courier) == (None, None, None, "Motero")  # COURIER_FALLBACK_NAME
    assert (courier.anonymized_at, courier.first_seen_at) == (NOW, OLD)
    # The old order keeps pointing at them.
    order = await get(sf, m.Order, person.history["order"])
    assert (order.store_id, order.courier_id, order.last_courier_id) == (
        person.store,
        person.courier,
        person.courier,
    )
    # No JID of theirs lingers in the old rows.
    async with engine.connect() as conn:
        for table, column in (
            ("inbound_events", "remote_jid"),
            ("inbound_events", "participant"),
            ("messages", "chat_jid"),
            ("messages", "sender_jid"),
            ("outbound_messages", "recipient_jid"),
        ):
            values = set(
                (await conn.execute(text(f"SELECT DISTINCT {column} FROM {table}"))).scalars()
            )
            assert not values & {STORE_JID, STORE_LID, JOEL_JID, JOEL_LID}, (table, column)
        names = await conn.scalar(text("SELECT snapshot::text FROM report_revisions"))
        assert "Joel" not in names


async def test_the_old_report_still_adds_up(ctx: dict[str, Any], person: Person) -> None:
    sf = ctx["session_factory"]
    async with sf() as session:
        couriers_before = await report_repo.load_courier_rows(session, OLD_DAY)
        orders_before = await report_repo.load_order_rows(session, OLD_DAY)
    await run(ctx)
    async with sf() as session:
        couriers_after = await report_repo.load_courier_rows(session, OLD_DAY)
        orders_after = await report_repo.load_order_rows(session, OLD_DAY)
    counts = [(c.courier_id, c.taken, c.released, c.cancelled) for c in couriers_after]
    assert counts == [(c.courier_id, c.taken, c.released, c.cancelled) for c in couriers_before]
    assert counts == [(person.courier, 1, 0, 0)]
    assert [(c.name, c.phone) for c in couriers_before] == [("Joel", "5491100000007")]
    assert [(c.name, c.phone) for c in couriers_after] == [("Motero", None)]
    assert [(o.code, o.status, o.store_name, o.courier_name) for o in orders_after] == [
        ("YV-0001", "assigned", "Local", "Motero")
    ]
    assert len(orders_after) == len(orders_before)


async def recent_activity(engine: AsyncEngine) -> dict[str, tuple[Any, int]]:
    """One identity per activity source (docs/06); each one alone keeps it."""
    base = await f.insert_ok(engine, f.store())  # active (created now)
    ids: dict[str, tuple[Any, int]] = {}

    async def store(name: str, **kw: Any) -> int:
        ids[name] = (m.Store, await f.insert_ok(engine, old_store(**kw)))
        return ids[name][1]

    async def courier(name: str, **kw: Any) -> int:
        ids[name] = (m.Courier, await f.insert_ok(engine, old_courier(**kw)))
        return ids[name][1]

    await store("store_created_recently", created_at=RECENT)
    await store("store_last_message_at", last_message_at=RECENT)
    await store("store_inbound_by_jid", wa_jid="5491155551111@s.whatsapp.net")
    await f.insert_ok(
        engine,
        f.inbound(
            status="processed", received_at=RECENT, remote_jid="5491155551111@s.whatsapp.net"
        ),
    )
    await store("store_inbound_by_lid", lid="333333333333333@lid")
    await f.insert_ok(
        engine,
        f.inbound(status="processed", received_at=RECENT, remote_jid="333333333333333@lid"),
    )
    s = await store("store_recent_order")
    await f.insert_ok(engine, f.published(s, status="cancelled", business_date=RECENT_DAY))
    s = await store("store_recent_message")
    await f.insert_ok(engine, f.message(store_id=s, created_at=RECENT))

    await courier("courier_last_seen", last_seen_at=RECENT)
    await courier("courier_reaction_by_lid", lid="444444444444444@lid")
    await f.insert_ok(
        engine,
        f.inbound(
            status="processed",
            received_at=RECENT,
            remote_jid=GROUP_JID,
            participant="444444444444444@lid",
        ),
    )
    await courier("courier_reaction_by_number", lid=None, phone_jid="5491100000055@s.whatsapp.net")
    await f.insert_ok(
        engine,
        f.inbound(
            status="processed",
            received_at=RECENT,
            remote_jid=GROUP_JID,
            participant="5491100000055@s.whatsapp.net",
        ),
    )
    c = await courier("courier_recent_event")
    order = await f.insert_ok(engine, f.published(base, business_date=RECENT_DAY))
    await f.insert_ok(
        engine,
        insert(m.OrderEvent).values(
            order_id=order,
            event_type="released_by_courier",
            actor_type="courier",
            actor_id=c,
            courier_id=c,
            created_at=RECENT,
        ),
    )
    c = await courier("courier_last_order")
    await f.insert_ok(
        engine,
        f.published(base, status="cancelled", business_date=RECENT_DAY, last_courier_id=c),
    )
    c = await courier("courier_recent_message")
    await f.insert_ok(engine, f.message(courier_id=c, created_at=RECENT))
    return ids


async def test_activity_inside_the_window_keeps_the_identity(
    ctx: dict[str, Any], person: Person, engine: AsyncEngine
) -> None:
    kept = await recent_activity(engine)
    before = {name: identity(await get(ctx["session_factory"], *row)) for name, row in kept.items()}

    counts = await run(ctx)

    assert (counts["stores"], counts["couriers"]) == (1, 1)  # only the person's
    for name, row in kept.items():
        after = await get(ctx["session_factory"], *row)
        assert (identity(after), after.anonymized_at) == (before[name], None), name


async def test_live_flows_keep_the_identity(ctx: dict[str, Any], engine: AsyncEngine) -> None:
    sf = ctx["session_factory"]
    kept: dict[str, tuple[Any, int]] = {}
    # A store with an old open draft, one awaiting confirmation, one published.
    for status in ("draft", "awaiting_confirmation", "published"):
        s = await f.insert_ok(engine, old_store())
        kept[f"store_{status}"] = (m.Store, s)
        code = "YV-P" if status == "published" else None
        await f.insert_ok(
            engine,
            f.order(
                s,
                status=status,
                business_date=OLD_DAY,
                created_at=OLD,
                code=code,
                published_at=OLD if code else None,
            ),
        )
    # A courier assigned on a day whose report is still being corrected (and its store).
    day = dt.date(2026, 6, 30)
    await f.insert_ok(engine, f.daily_report(report_date=day, status="correction_pending"))
    s = await f.insert_ok(engine, old_store())
    c = await f.insert_ok(engine, old_courier())
    kept |= {"store_open_day": (m.Store, s), "courier_open_day": (m.Courier, c)}
    await f.insert_ok(
        engine,
        f.published(
            s, status="assigned", courier_id=c, last_courier_id=c, business_date=day, created_at=OLD
        ),
    )
    # A courier with a reaction still parked, and a store with a message still in the outbox.
    c = await f.insert_ok(engine, old_courier())
    kept["courier_parked"] = (m.Courier, c)
    await f.insert_ok(
        engine,
        insert(m.ParkedReaction).values(
            group_message_id="NOTYET", kind="added", courier_id=c, emoji="👍", reacted_at=OLD
        ),
    )
    s = await f.insert_ok(engine, old_store(wa_jid="5491155552222@s.whatsapp.net"))
    kept["store_outbox"] = (m.Store, s)
    await f.insert_ok(
        engine,
        f.outbound(recipient_jid="5491155552222@s.whatsapp.net", status="pending", created_at=OLD),
    )
    # Admins (by phone or by JID) and blocked identities.
    s = await f.insert_ok(engine, old_store(phone=ADMIN_PHONE))
    kept["store_admin_phone"] = (m.Store, s)
    s = await f.insert_ok(engine, old_store(wa_jid="5491100000002@s.whatsapp.net"))
    kept["store_admin_jid"] = (m.Store, s)
    c = await f.insert_ok(engine, old_courier(phone_jid=ADMIN_JID))
    kept["courier_admin"] = (m.Courier, c)
    kept["store_blocked"] = (m.Store, await f.insert_ok(engine, old_store(is_blocked=True)))
    kept["courier_blocked"] = (m.Courier, await f.insert_ok(engine, old_courier(is_blocked=True)))
    before = {name: identity(await get(sf, *row)) for name, row in kept.items()}

    counts = await run(ctx)

    assert (counts["stores"], counts["couriers"]) == (0, 0)
    for name, row in kept.items():
        after = await get(sf, *row)
        assert (identity(after), after.anonymized_at) == (before[name], None), name


async def test_admin_and_group_jids_survive_in_old_rows(
    ctx: dict[str, Any], engine: AsyncEngine
) -> None:
    sf = ctx["session_factory"]
    to_admin = await f.insert_ok(
        engine, f.outbound(recipient_jid=ADMIN_JID, status="sent", created_at=OLD)
    )
    to_group = await f.insert_ok(
        engine, f.outbound(recipient_jid=GROUP_JID, status="sent", created_at=OLD)
    )
    override = await f.insert_ok(
        engine, f.override(status="expired", created_by=ADMIN_PHONE, created_at=OLD)
    )
    await run(ctx)
    assert (await get(sf, m.OutboundMessage, to_admin)).recipient_jid == ADMIN_JID
    assert (await get(sf, m.OutboundMessage, to_group)).recipient_jid == GROUP_JID
    kept = await get(sf, m.ScheduleOverride, override)
    assert (kept.created_by, kept.source_text) == (ADMIN_PHONE, "")


# --- after anonymization -----------------------------------------------------------------------


async def test_the_same_person_comes_back_as_a_new_record(
    ctx: dict[str, Any], person: Person, engine: AsyncEngine
) -> None:
    """The planned registration (docs/04 §1-2, docs/05: lookup by every identifier, else
    INSERT) finds nothing, so a new store and courier are created; old data keeps the old ids."""
    sf = ctx["session_factory"]
    settings = ctx["settings"]
    await run(ctx)

    async with sf() as session, session.begin():
        store_id = await session.scalar(
            text(
                """INSERT INTO stores (wa_jid, phone, lid, display_name)
                   VALUES (:jid, '5491155550000', :lid, 'Casa Nogal Nuevo')
                   ON CONFLICT (wa_jid) DO UPDATE SET display_name = EXCLUDED.display_name
                   RETURNING id"""
            ),
            {"jid": STORE_JID, "lid": STORE_LID},
        )
        found = await session.scalar(
            text("SELECT id FROM couriers WHERE lid = :lid OR phone_jid = :jid FOR UPDATE"),
            {"lid": JOEL_LID, "jid": JOEL_JID},
        )
        assert found is None
        courier_id = await session.scalar(
            insert(m.Courier)
            .values(lid=JOEL_LID, phone_jid=JOEL_JID, phone="5491100000007", display_name="Joel")
            .returning(m.Courier.id)
        )
    assert store_id != person.store and courier_id != person.courier
    async with sf() as session:
        assert await display_name_for_jid(session, settings, STORE_JID) == "Casa Nogal Nuevo"
        assert await display_name_for_jid(session, settings, JOEL_JID) == "Joel"

    # The new records work in a live flow: a claim today notifies the courier and the store.
    order = await f.insert_ok(
        engine,
        f.published(
            store_id, code="YV-0002", business_date=TODAY, published_at=NOW, group_message_id=POST
        ),
    )
    result = await order_jobs.handle_reaction(
        ctx, ReactionAdded(group_message_id=POST, courier_id=courier_id, emoji="👍")
    )
    assert (result.outcome, result.order_id) == ("claimed", order)
    async with sf() as session:
        recipients = (
            await session.scalars(
                select(m.OutboundMessage.recipient_jid).where(m.OutboundMessage.order_id == order)
            )
        ).all()
    assert sorted(recipients) == sorted([JOEL_JID, STORE_JID, GROUP_JID])

    # The old report still points at the anonymized courier.
    async with sf() as session:
        rows = await report_repo.load_courier_rows(session, OLD_DAY)
    assert [(r.courier_id, r.name, r.taken) for r in rows] == [(person.courier, "Motero", 1)]
    # And a later run leaves the new, active records alone.
    assert (await run(ctx))["couriers"] == 0
    assert (await get(sf, m.Courier, courier_id)).phone_jid == JOEL_JID


async def test_a_message_to_an_anonymized_courier_is_skipped(
    ctx: dict[str, Any], person: Person, caplog: pytest.LogCaptureFixture
) -> None:
    """Defensive: nothing live references an anonymized identity, but if an admin acted on an
    old order the outbox has no JID to write to and skips it instead of failing."""
    from ya_voy.orders.notify import Outbox, courier_jid

    await run(ctx)
    courier = await get(ctx["session_factory"], m.Courier, person.courier)
    assert courier_jid(courier) is None
    async with ctx["session_factory"]() as session, session.begin():
        new_id = await Outbox(session).add("k:1", "courier_order_released", courier_jid(courier))
    assert new_id is None
    assert "outbound skipped: recipient anonymized" in caplog.text


async def test_a_second_run_changes_nothing(
    ctx: dict[str, Any], person: Person, clock: Clock
) -> None:
    await run(ctx)
    clock.now = NOW + dt.timedelta(hours=1)
    assert set((await run(ctx)).values()) == {0}
    store = await get(ctx["session_factory"], m.Store, person.store)
    assert store.anonymized_at == NOW  # not re-stamped


async def test_a_locked_identity_is_left_for_the_next_run(
    ctx: dict[str, Any], person: Person
) -> None:
    """A registration resolving the courier holds its row (``FOR UPDATE``): skipped, not waited."""
    sf = ctx["session_factory"]
    async with sf() as session, session.begin():
        await session.execute(
            text("SELECT id FROM couriers WHERE lid = :lid FOR UPDATE"), {"lid": JOEL_LID}
        )
        counts = await asyncio.wait_for(run(ctx), 10)
    assert (counts["stores"], counts["couriers"]) == (1, 0)
    assert (await run(ctx))["couriers"] == 1


async def test_a_registration_waiting_on_retention_creates_a_new_record(
    ctx: dict[str, Any], person: Person, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retention holds the courier row; the resolver's ``FOR UPDATE`` waits, then re-checks the
    row (now without identifiers) and finds nothing: it inserts a new courier."""
    sf = ctx["session_factory"]
    locked = asyncio.Event()
    release = asyncio.Event()
    original = service.anonymize_batch

    async def paused(session: AsyncSession, category: Any, params: dict[str, Any]) -> int:
        done = await original(session, category, params)
        if category.name == "couriers" and done:
            locked.set()
            await release.wait()  # still inside the batch transaction
        return done

    monkeypatch.setattr(service, "anonymize_batch", paused)
    retention = asyncio.create_task(run(ctx))
    try:
        await asyncio.wait_for(locked.wait(), 10)

        async def resolve() -> Any:
            async with sf() as session, session.begin():
                return await session.scalar(
                    text("SELECT id FROM couriers WHERE lid = :lid FOR UPDATE"), {"lid": JOEL_LID}
                )

        resolver = asyncio.create_task(resolve())
        await asyncio.sleep(0.3)
        assert not resolver.done()  # waiting on the row lock
    finally:
        release.set()
    assert (await retention)["couriers"] == 1
    assert await asyncio.wait_for(resolver, 10) is None


@pytest.mark.parametrize("attempt", range(3))
async def test_concurrent_runs_anonymize_each_identity_once(
    worker_ctx: Ctx, engine: AsyncEngine, tmp_path: Any, attempt: int
) -> None:
    ctx = worker_ctx(data_retention_batch_size=3, capture_dir=str(tmp_path))
    async with engine.begin() as conn:
        for _ in range(20):
            await conn.execute(old_store())
            await conn.execute(old_courier())
    before = await count_rows(engine)
    results = await asyncio.gather(*(run(ctx) for _ in range(4)))
    assert sum(r["stores"] for r in results) == 20
    assert sum(r["couriers"] for r in results) == 20
    assert await count_rows(engine) == before
    async with engine.connect() as conn:
        left = await conn.scalar(
            select(func.count()).select_from(m.Courier).where(m.Courier.lid.is_not(None))
        )
    assert left == 0


async def test_unique_identifiers_allow_many_anonymized_rows(
    ctx: dict[str, Any], engine: AsyncEngine
) -> None:
    async with engine.begin() as conn:
        for _ in range(3):
            await conn.execute(old_store(phone=None))
            await conn.execute(old_courier())
    counts = await run(ctx)
    assert (counts["stores"], counts["couriers"]) == (3, 3)
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM stores WHERE wa_jid IS NULL")) == 3
        assert await conn.scalar(text("SELECT count(*) FROM couriers WHERE lid IS NULL")) == 3


async def test_only_anonymized_identities_may_lack_identifiers(engine: AsyncEngine) -> None:
    await f.assert_rejected(engine, f.store(wa_jid=None), "stores_has_identity")
    await f.assert_rejected(engine, f.courier(lid=None, phone_jid=None), "couriers_has_identity")
    await f.insert_ok(engine, f.store(wa_jid=None, anonymized_at=NOW))
    await f.insert_ok(engine, f.courier(lid=None, phone_jid=None, anonymized_at=NOW))
