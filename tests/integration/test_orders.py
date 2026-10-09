"""Publication, claims, releases and cancellations against Postgres/Redis (stages 5a/6a).

Events arrive already normalized (``ReactionAdded`` / ``ReactionRemoved`` with a resolved
courier); store commands go through ``drafts.handle_store_text`` like a real private text.
"Now" is frozen through ``schedule.repository.db_now``; Evolution is mocked with respx. Every
expected WhatsApp text is written out literally (doc 07) so a catalog drift fails here.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tests.integration import factories as f
from tests.integration.conftest import EVOLUTION_URL, queued
from tests.llm_stubs import StubExtractor
from tests.schedule_helpers import at
from ya_voy.db import models as m
from ya_voy.orders import cancellation, claims, parking
from ya_voy.orders.events import ReactionAdded, ReactionRemoved
from ya_voy.queue import SEND_OUTBOUND
from ya_voy.schedule import repository as schedule_repository
from ya_voy.worker import drafts
from ya_voy.worker import orders as order_jobs
from ya_voy.worker.intake import StoreText
from ya_voy.worker.outbound import send_outbound

Ctx = Callable[..., dict[str, Any]]
SF = async_sessionmaker[AsyncSession]
D = dt.date(2026, 10, 9)
GROUP = "120363000000000001@g.us"
POST = "GROUPPOST1"
STORE_JID = "5491155550000@s.whatsapp.net"
ADMIN_JIDS = ("5491100000001@s.whatsapp.net", "5491100000002@s.whatsapp.net")
DETAILS = (
    "🏪 *Local:* Casa Nogal\n"
    "📍 *Retiro:* casa nogal almendros\n"
    "📍 *Entrega:* el ceibo 2871\n"
    "🥪 *Producto:* almacen\n"
    "💳 *Abona:* transferencia"
)


class Clock:
    def __init__(self, now: dt.datetime) -> None:
        self.now = now

    async def __call__(self, session: AsyncSession) -> dt.datetime:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock(at(9, 15))
    monkeypatch.setattr(schedule_repository, "db_now", c)
    return c


@pytest.fixture
def evo() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=EVOLUTION_URL, assert_all_called=False) as router:
        counter = iter(range(1, 10_000))

        def sent(request: httpx.Request) -> httpx.Response:
            return httpx.Response(201, json={"key": {"id": f"BAE5{next(counter)}", "fromMe": True}})

        router.post("/message/sendText/yavoy", name="sendText").mock(side_effect=sent)
        router.delete("/chat/deleteMessageForEveryone/yavoy", name="delete").mock(side_effect=sent)
        yield router


@pytest.fixture
def ctx(worker_ctx: Ctx, clock: Clock, evo: respx.MockRouter) -> dict[str, Any]:
    c = worker_ctx()
    c["order_extractor"] = StubExtractor({})
    return c


# --- data ----------------------------------------------------------------------------------


class World:
    """One store, couriers by name and orders built directly in the documented states."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.store_id = 0
        self.couriers: dict[str, int] = {}

    async def setup(self, riders: int = 3) -> World:
        self.store_id = await f.insert_ok(
            self.engine, f.store(display_name="Casa Nogal", wa_jid=STORE_JID)
        )
        for i in range(riders):
            name = f"Motero{i}"
            self.couriers[name] = await f.insert_ok(
                self.engine,
                f.courier(
                    display_name=name,
                    phone=f"54911200000{i:02d}",
                    phone_jid=f"54911200000{i:02d}@s.whatsapp.net",
                ),
            )
        return self

    def jid(self, name: str) -> str:
        return f"54911200000{int(name.removeprefix('Motero')):02d}@s.whatsapp.net"

    async def order(
        self,
        status: str = "published",
        *,
        code: str | None = "YV-0001",
        courier: str | None = None,
        group_message_id: str | None = POST,
    ) -> int:
        published = status in ("published", "assigned", "cancelled")
        values: dict[str, Any] = {
            "status": status,
            "business_date": D,
            "code": code if published else None,
            "pickup_address": "casa nogal almendros",
            "delivery_address": "el ceibo 2871",
            "product": "almacen",
            "payment_method": "transfer",
            "payment_method_raw": "transferencia",
            "group_message_id": group_message_id if published else None,
            "published_at": at(9, 14, 30) if published else None,
            "confirmed_at": at(9, 14, 30) if published else None,
            "unclaimed_alert_at": at(9, 14, 50) if status == "published" else None,
            "courier_id": self.couriers[courier] if courier else None,
            "last_courier_id": self.couriers[courier] if courier else None,
            "assigned_at": at(9, 14, 55) if courier else None,
        }
        return await f.insert_ok(self.engine, f.order(self.store_id, **values))

    async def inbound(self) -> int:
        return await f.insert_ok(self.engine, f.inbound())


@pytest.fixture
async def world(engine: AsyncEngine) -> World:
    return await World(engine).setup()


async def get_order(sf: SF, order_id: int) -> m.Order:
    async with sf() as session:
        order = await session.get(m.Order, order_id)
        assert order is not None
        return order


async def outbox(sf: SF, ids: list[int] | None = None) -> list[m.OutboundMessage]:
    async with sf() as session:
        stmt = select(m.OutboundMessage).order_by(m.OutboundMessage.id)
        if ids is not None:
            stmt = stmt.where(m.OutboundMessage.id.in_(ids))
        return list(await session.scalars(stmt))


def messages(rows: list[m.OutboundMessage]) -> list[tuple[str, str, str]]:
    return [(r.template_key, r.recipient_jid, r.body) for r in rows]


async def events(sf: SF, order_id: int) -> list[tuple[str, int | None]]:
    async with sf() as session:
        rows = await session.scalars(
            select(m.OrderEvent).where(m.OrderEvent.order_id == order_id).order_by(m.OrderEvent.id)
        )
        return [(e.event_type, e.courier_id) for e in rows]


async def react(world: World, ctx: dict[str, Any], name: str, *, emoji: str = "👍", **kw: Any):
    event = ReactionAdded(kw.pop("post", POST), world.couriers[name], emoji, **kw)
    return await order_jobs.handle_reaction(ctx, event)


async def unreact(world: World, ctx: dict[str, Any], name: str, **kw: Any):
    event = ReactionRemoved(kw.pop("post", POST), world.couriers[name], **kw)
    return await order_jobs.handle_reaction(ctx, event)


async def store_says(ctx: dict[str, Any], world: World, text: str, **kw: Any):
    return await drafts.handle_store_text(ctx, StoreText(store_id=world.store_id, text=text, **kw))


async def deliver(ctx: dict[str, Any]) -> list[str]:
    results = []
    for row in await outbox(ctx["session_factory"]):
        if row.status == "pending":
            results.append(await send_outbound(ctx, row.id))
    return results


def assigned_messages(world: World, name: str) -> list[tuple[str, str, str]]:
    return [
        (
            "courier_order_assigned",
            world.jid(name),
            f"✅ *Te asignamos el pedido YV-0001*\n\n{DETAILS}\n\n"
            "Si no podés hacerlo, quitá tu reacción en el grupo para liberarlo.",
        ),
        ("store_order_assigned", STORE_JID, f"🏍️ Tu motero para el pedido *YV-0001* será *{name}*."),
        ("group_order_assigned", GROUP, f"✅ *YV-0001* asignado a *{name}*."),
    ]


def released_messages(world: World, name: str) -> list[tuple[str, str, str]]:
    return [
        (
            "store_order_unassigned",
            STORE_JID,
            f"⚠️ {name} liberó tu pedido *YV-0001*. Ya está disponible de nuevo para los moteros.",
        ),
        (
            "group_order_unassigned",
            GROUP,
            "🔓 *YV-0001* vuelve a estar disponible. Reaccioná de nuevo para tomarlo "
            "(si ya habías reaccionado, cambiá el emoji).",
        ),
        (
            "courier_order_released",
            world.jid(name),
            "Liberaste el pedido *YV-0001*; ya no está a tu nombre.",
        ),
    ]


# --- claim (T10, T11, T12) -------------------------------------------------------------------


async def test_a_claim_assigns_and_notifies_courier_store_and_group(
    ctx: dict[str, Any], world: World
) -> None:
    order_id = await world.order()
    result = await react(world, ctx, "Motero0")

    assert (result.outcome, result.order_id) == ("claimed", order_id)
    order = await get_order(ctx["session_factory"], order_id)
    assert (order.status, order.courier_id, order.last_courier_id, order.assigned_at) == (
        "assigned",
        world.couriers["Motero0"],
        world.couriers["Motero0"],
        at(9, 15),
    )
    rows = await outbox(ctx["session_factory"])
    assert messages(rows) == assigned_messages(world, "Motero0")
    assert [r.id for r in rows] == result.outbound_ids
    group_reply = rows[2]
    assert group_reply.quoted_wa_message_id == POST
    assert await events(ctx["session_factory"], order_id) == [
        ("claimed", world.couriers["Motero0"])
    ]
    assert sorted(a[1][0] for a in await queued(ctx["redis"], SEND_OUTBOUND)) == sorted(
        result.outbound_ids
    )


@pytest.mark.parametrize("attempt", range(5))
async def test_n_riders_reacting_concurrently_one_assignment(
    worker_ctx: Ctx, clock: Clock, evo: respx.MockRouter, engine: AsyncEngine, attempt: int
) -> None:
    ctx = worker_ctx()
    world = await World(engine).setup(riders=10)
    order_id = await world.order()

    results = await asyncio.gather(*(react(world, ctx, name) for name in world.couriers))

    outcomes = sorted(r.outcome for r in results)
    assert outcomes == ["claimed"] + ["rejected:already_taken"] * 9
    winner = next(n for n, r in zip(world.couriers, results, strict=True) if r.outcome == "claimed")
    order = await get_order(ctx["session_factory"], order_id)
    assert (order.status, order.courier_id) == ("assigned", world.couriers[winner])

    rows = await outbox(ctx["session_factory"])
    taken = [r for r in rows if r.template_key == "courier_order_already_taken"]
    assert sorted(r.recipient_jid for r in taken) == sorted(
        world.jid(n) for n in world.couriers if n != winner
    )
    assert {r.body for r in taken} == {
        "El pedido *YV-0001* ya lo tomó otro motero. ¡Gracias igual!"
    }
    assert messages([r for r in rows if r.template_key != "courier_order_already_taken"]) == (
        assigned_messages(world, winner)
    )
    evs = await events(ctx["session_factory"], order_id)
    assert evs.count(("claimed", world.couriers[winner])) == 1
    assert sorted(e for e in evs if e[0] == "claim_rejected") == sorted(
        ("claim_rejected", world.couriers[n]) for n in world.couriers if n != winner
    )


async def test_loser_notice_can_be_turned_off(
    worker_ctx: Ctx, clock: Clock, evo: respx.MockRouter, world: World
) -> None:
    ctx = worker_ctx(notify_loser_on_race=False)
    await world.order(status="assigned", courier="Motero0")
    result = await react(world, ctx, "Motero1")
    assert (result.outcome, result.outbound_ids) == ("rejected:already_taken", [])


async def test_emoji_change_by_the_assigned_courier_changes_nothing(
    ctx: dict[str, Any], world: World
) -> None:
    order_id = await world.order()
    await react(world, ctx, "Motero0", emoji="👍")
    before = await outbox(ctx["session_factory"])

    # WhatsApp sends a new reaction with the new emoji (no removal in between): T12.
    result = await react(world, ctx, "Motero0", emoji="🏍️")

    assert (result.outcome, result.outbound_ids) == ("ignored:same_courier", [])
    assert len(await outbox(ctx["session_factory"])) == len(before)
    order = await get_order(ctx["session_factory"], order_id)
    assert (order.status, order.courier_id) == ("assigned", world.couriers["Motero0"])
    assert await events(ctx["session_factory"], order_id) == [
        ("claimed", world.couriers["Motero0"])
    ]


async def test_emoji_change_by_a_loser_is_told_once(ctx: dict[str, Any], world: World) -> None:
    order_id = await world.order(status="assigned", courier="Motero0")
    first = await react(world, ctx, "Motero1", emoji="👍")
    second = await react(world, ctx, "Motero1", emoji="🔥")

    assert (first.outcome, second.outcome) == ("rejected:already_taken", "rejected:already_taken")
    assert len(first.outbound_ids) == 1 and second.outbound_ids == []  # once per courier/order
    order = await get_order(ctx["session_factory"], order_id)
    assert order.courier_id == world.couriers["Motero0"]


async def test_reactions_on_other_messages_and_by_blocked_couriers_are_ignored(
    ctx: dict[str, Any], world: World, engine: AsyncEngine
) -> None:
    order_id = await world.order()
    await f.insert_ok(
        engine,
        f.outbound(
            template_key="group_order_assigned",
            recipient_jid=GROUP,
            order_id=order_id,
            status="sent",
            wa_message_id="BOT-REPLY",
        ),
    )
    # RN-12: a bot reply that WhatsApp already has an id for is not a post; nothing to wait for.
    assert (await react(world, ctx, "Motero0", post="BOT-REPLY")).outcome == (
        "ignored:not_an_order_post"
    )
    async with engine.begin() as conn:
        await conn.execute(
            update(m.Courier)
            .where(m.Courier.id == world.couriers["Motero1"])
            .values(is_blocked=True)
        )
    assert (await react(world, ctx, "Motero1")).outcome == "ignored:courier_blocked"
    order = await get_order(ctx["session_factory"], order_id)
    assert (order.status, order.courier_id) == ("published", None)
    assert len(await outbox(ctx["session_factory"])) == 1  # only the bot reply


# --- release (T13, T14) and retake ------------------------------------------------------------


async def test_release_by_the_assigned_courier(
    ctx: dict[str, Any], world: World, clock: Clock
) -> None:
    order_id = await world.order()
    await react(world, ctx, "Motero0")
    clock.now = at(9, 15, 20)

    result = await unreact(world, ctx, "Motero0")

    assert result.outcome == "released"
    order = await get_order(ctx["session_factory"], order_id)
    assert (order.status, order.courier_id, order.assigned_at) == ("published", None, None)
    assert order.last_courier_id == world.couriers["Motero0"]
    assert order.unclaimed_alert_at == at(9, 15, 20) + dt.timedelta(minutes=20)
    assert order.unclaimed_alert_sent_at is None
    rows = await outbox(ctx["session_factory"], result.outbound_ids)
    assert messages(rows) == released_messages(world, "Motero0")
    assert rows[1].quoted_wa_message_id == POST


async def test_release_by_a_non_assigned_courier_is_ignored(
    ctx: dict[str, Any], world: World
) -> None:
    order_id = await world.order()
    await react(world, ctx, "Motero0")
    await react(world, ctx, "Motero1")  # lost the race
    count = len(await outbox(ctx["session_factory"]))

    result = await unreact(world, ctx, "Motero1")

    assert (result.outcome, result.outbound_ids) == ("ignored:not_assigned_to_remover", [])
    order = await get_order(ctx["session_factory"], order_id)
    assert (order.status, order.courier_id) == ("assigned", world.couriers["Motero0"])
    assert len(await outbox(ctx["session_factory"])) == count
    assert [e[0] for e in await events(ctx["session_factory"], order_id)] == [
        "claimed",
        "claim_rejected",
    ]


async def test_removal_on_a_published_order_is_ignored(ctx: dict[str, Any], world: World) -> None:
    order_id = await world.order()
    result = await unreact(world, ctx, "Motero0")
    assert result.outcome == "ignored:not_assigned_to_remover"
    assert (await get_order(ctx["session_factory"], order_id)).status == "published"


async def test_release_then_retake_notifies_again(ctx: dict[str, Any], world: World) -> None:
    order_id = await world.order()
    first = await react(world, ctx, "Motero0")
    released = await unreact(world, ctx, "Motero0")
    other = await react(world, ctx, "Motero1")
    back = await unreact(world, ctx, "Motero1")
    again = await react(world, ctx, "Motero0", emoji="🔥")

    assert [r.outcome for r in (first, released, other, back, again)] == [
        "claimed",
        "released",
        "claimed",
        "released",
        "claimed",
    ]
    assert messages(await outbox(ctx["session_factory"], again.outbound_ids)) == (
        assigned_messages(world, "Motero0")
    )
    assert messages(await outbox(ctx["session_factory"], other.outbound_ids)) == (
        assigned_messages(world, "Motero1")
    )
    order = await get_order(ctx["session_factory"], order_id)
    assert (order.status, order.courier_id) == ("assigned", world.couriers["Motero0"])
    assert [e[0] for e in await events(ctx["session_factory"], order_id)] == [
        "claimed",
        "released_by_courier",
        "claimed",
        "released_by_courier",
        "claimed",
    ]


@pytest.mark.parametrize("attempt", range(3))
async def test_concurrent_release_and_retake_by_others(
    ctx: dict[str, Any], world: World, attempt: int
) -> None:
    """The release and two other couriers' reactions race: at most one assignment ever, and
    the release is applied exactly once."""
    order_id = await world.order()
    await react(world, ctx, "Motero0")
    results = await asyncio.gather(
        unreact(world, ctx, "Motero0"), react(world, ctx, "Motero1"), react(world, ctx, "Motero2")
    )
    assert results[0].outcome == "released"
    claimed = [r for r in results[1:] if r.outcome == "claimed"]
    assert len(claimed) <= 1
    order = await get_order(ctx["session_factory"], order_id)
    if claimed:
        assert order.status == "assigned"
        assert order.courier_id in (world.couriers["Motero1"], world.couriers["Motero2"])
    else:
        assert (order.status, order.courier_id) == ("published", None)


# --- duplicates ----------------------------------------------------------------------------


async def test_duplicate_events_are_idempotent(ctx: dict[str, Any], world: World) -> None:
    order_id = await world.order()
    claim_event = await world.inbound()
    first = await react(world, ctx, "Motero0", inbound_event_id=claim_event)
    again = await react(world, ctx, "Motero0", inbound_event_id=claim_event)
    assert (first.outcome, again.outcome, again.outbound_ids) == ("claimed", "duplicate", [])

    removal = await world.inbound()
    released = await unreact(world, ctx, "Motero0", inbound_event_id=removal)
    retake = await react(world, ctx, "Motero0", inbound_event_id=await world.inbound())
    # The removal redelivered after the retake must not release the new claim.
    stale = await unreact(world, ctx, "Motero0", inbound_event_id=removal)
    assert (released.outcome, retake.outcome, stale.outcome) == ("released", "claimed", "duplicate")

    order = await get_order(ctx["session_factory"], order_id)
    assert (order.status, order.courier_id) == ("assigned", world.couriers["Motero0"])
    assert [e[0] for e in await events(ctx["session_factory"], order_id)] == [
        "claimed",
        "released_by_courier",
        "claimed",
    ]
    assert len(await outbox(ctx["session_factory"])) == 9


async def test_concurrent_duplicates_of_one_event_apply_once(
    ctx: dict[str, Any], world: World
) -> None:
    order_id = await world.order()
    inbound_id = await world.inbound()
    results = await asyncio.gather(
        *(react(world, ctx, "Motero0", inbound_event_id=inbound_id) for _ in range(5))
    )
    assert sorted(r.outcome for r in results).count("claimed") == 1
    assert len(await outbox(ctx["session_factory"])) == 3
    assert [e[0] for e in await events(ctx["session_factory"], order_id)] == ["claimed"]


# --- cancel vs claim (T16, T17, T19) -----------------------------------------------------------


TAKEN = "El pedido *YV-0001* ya lo tomó otro motero. ¡Gracias igual!"
STORE_CANCELLED = "❌ Pedido *YV-0001* cancelado. Ya lo saqué del grupo de moteros."
COURIER_CANCELLED = (
    f"❌ Tu pedido *YV-0001* ha sido cancelado:\n\n{DETAILS}\n\n"
    "No hace falta que lo retires. Si ya lo retiraste, devolvelo en el local."
)
NO_LONGER = "El pedido *YV-0001* fue cancelado y ya no está disponible."


async def test_claim_then_cancel_tells_the_courier(ctx: dict[str, Any], world: World) -> None:
    order_id = await world.order()
    await react(world, ctx, "Motero0")
    await deliver(ctx)  # the group reply now has a WhatsApp id

    result = await store_says(ctx, world, "cancelar")

    assert (result.outcome, result.order_id) == ("cancelled_assigned", order_id)
    order = await get_order(ctx["session_factory"], order_id)
    assert (order.status, order.courier_id, order.cancelled_by) == ("cancelled", None, "store")
    assert order.last_courier_id == world.couriers["Motero0"]
    rows = await outbox(ctx["session_factory"], result.outbound_ids)
    group_reply = next(
        r for r in await outbox(ctx["session_factory"]) if r.template_key == "group_order_assigned"
    )
    assert [(r.template_key, r.message_type, r.target_wa_message_id) for r in rows[:2]] == [
        ("group_order_delete", "delete", POST),
        ("group_order_delete", "delete", group_reply.wa_message_id),
    ]
    assert messages(rows[2:]) == [
        ("courier_order_cancelled", world.jid("Motero0"), COURIER_CANCELLED),
        ("store_order_cancelled", STORE_JID, STORE_CANCELLED + "\nYa le avisé a Motero0."),
    ]
    assert [e for e in await events(ctx["session_factory"], order_id)][-1] == (
        "cancelled",
        world.couriers["Motero0"],
    )


async def test_cancel_then_claim_is_no_longer_available(ctx: dict[str, Any], world: World) -> None:
    order_id = await world.order()
    cancelled = await store_says(ctx, world, "cancelar")
    assert cancelled.outcome == "cancelled"
    assert messages(await outbox(ctx["session_factory"], cancelled.outbound_ids)) == [
        ("group_order_delete", GROUP, ""),
        ("store_order_cancelled", STORE_JID, STORE_CANCELLED),
    ]

    late = await react(world, ctx, "Motero0")
    again = await react(world, ctx, "Motero0", emoji="🔥")

    assert (late.outcome, again.outcome) == ("ignored:cancelled", "ignored:cancelled")
    assert messages(await outbox(ctx["session_factory"], late.outbound_ids)) == [
        ("courier_order_no_longer_available", world.jid("Motero0"), NO_LONGER)
    ]
    assert again.outbound_ids == []  # once per courier and order
    order = await get_order(ctx["session_factory"], order_id)
    assert (order.status, order.courier_id) == ("cancelled", None)
    assert [e[0] for e in await events(ctx["session_factory"], order_id)] == [
        "cancelled",
        "reaction_on_cancelled",
        "reaction_on_cancelled",
    ]
    assert (await unreact(world, ctx, "Motero0")).outcome == "ignored:not_assigned_to_remover"


async def test_claim_waits_for_a_cancel_holding_the_row(ctx: dict[str, Any], world: World) -> None:
    """Cancel locked the row first: the claim's UPDATE blocks, then finds ``cancelled`` (T19)."""
    order_id = await world.order()
    sf = ctx["session_factory"]
    async with sf() as session, session.begin():
        order = await session.scalar(
            select(m.Order).where(m.Order.id == order_id).with_for_update()
        )
        assert order is not None
        claim = asyncio.create_task(react(world, ctx, "Motero0"))
        await asyncio.sleep(0.3)
        assert not claim.done()  # blocked on the row lock
        result = await cancellation.cancel_locked(
            session, ctx["settings"], order, actor="store", actor_id=world.store_id
        )
        assert result.outcome == "cancelled"
    late = await claim
    assert late.outcome == "ignored:cancelled"
    order = await get_order(sf, order_id)
    assert (order.status, order.courier_id) == ("cancelled", None)
    assert [r.template_key for r in await outbox(sf)] == [
        "group_order_delete",
        "store_order_cancelled",
        "courier_order_no_longer_available",
    ]


async def test_cancel_waits_for_a_claim_holding_the_row(ctx: dict[str, Any], world: World) -> None:
    """The claim's UPDATE is not committed yet: the cancel's FOR UPDATE blocks, then reads
    ``assigned`` and goes through T17 (the courier is told)."""
    order_id = await world.order()
    sf = ctx["session_factory"]
    async with sf() as session, session.begin():
        claim = await parking.handle_reaction(
            session, ctx["settings"], ReactionAdded(POST, world.couriers["Motero0"], "👍")
        )
        assert claim.outcome == "claimed"
        cancel = asyncio.create_task(store_says(ctx, world, "cancelar"))
        await asyncio.sleep(0.3)
        assert not cancel.done()
    result = await cancel
    assert result.outcome == "cancelled_assigned"
    order = await get_order(sf, order_id)
    assert (order.status, order.courier_id, order.last_courier_id) == (
        "cancelled",
        None,
        world.couriers["Motero0"],
    )
    templates = [r.template_key for r in await outbox(sf, result.outbound_ids)]
    assert "courier_order_cancelled" in templates


@pytest.mark.parametrize("attempt", range(8))
async def test_claims_and_cancel_concurrently_stay_consistent(
    ctx: dict[str, Any], world: World, attempt: int
) -> None:
    order_id = await world.order()
    results = await asyncio.gather(
        react(world, ctx, "Motero0"),
        store_says(ctx, world, "cancelar"),
        react(world, ctx, "Motero1"),
    )
    claim_results = [results[0], results[2]]
    cancel = results[1]
    order = await get_order(ctx["session_factory"], order_id)
    assert (order.status, order.courier_id) == ("cancelled", None)
    rows = await outbox(ctx["session_factory"])
    templates = [r.template_key for r in rows]
    winners = [r for r in claim_results if r.outcome == "claimed"]
    assert len(winners) <= 1
    if cancel.outcome == "cancelled_assigned":  # a claim committed first
        assert len(winners) == 1
        assert templates.count("courier_order_cancelled") == 1
        assert order.last_courier_id is not None
    else:
        assert cancel.outcome == "cancelled"
        assert winners == []
        assert "courier_order_cancelled" not in templates
    for r in claim_results:
        assert r.outcome in ("claimed", "rejected:already_taken", "ignored:cancelled")
    assert templates.count("store_order_cancelled") == 1


# --- report correction (8a TODO) --------------------------------------------------------------


async def report_status(sf: SF) -> str:
    async with sf() as session:
        report = await session.scalar(select(m.DailyReport).where(m.DailyReport.report_date == D))
        assert report is not None
        return report.status


@pytest.mark.parametrize("action", ["release", "store_cancel", "admin_cancel", "admin_release"])
async def test_changes_to_an_order_in_a_sent_report_request_a_correction(
    ctx: dict[str, Any], world: World, engine: AsyncEngine, action: str
) -> None:
    await world.order(status="assigned", courier="Motero0")
    await f.insert_ok(engine, f.daily_report(status="sent"))
    sf = ctx["session_factory"]
    if action == "release":
        assert (await unreact(world, ctx, "Motero0")).outcome == "released"
    elif action == "store_cancel":
        assert (await store_says(ctx, world, "cancelar")).outcome == "cancelled_assigned"
    else:
        async with sf() as session, session.begin():
            fn = (
                cancellation.cancel_by_admin
                if action == "admin_cancel"
                else claims.release_by_admin
            )
            result = await fn(
                session,
                ctx["settings"],
                code="yv-0001",
                admin_jid=ADMIN_JIDS[0],
                request_key="test:1",
            )
        assert result.outcome in ("cancelled_assigned", "released")
    assert await report_status(sf) == "correction_pending"


async def test_ignored_events_do_not_touch_the_report(
    ctx: dict[str, Any], world: World, engine: AsyncEngine
) -> None:
    await world.order(status="assigned", courier="Motero0")
    await f.insert_ok(engine, f.daily_report(status="sent"))
    await unreact(world, ctx, "Motero1")
    await react(world, ctx, "Motero1")
    assert await report_status(ctx["session_factory"]) == "sent"


# --- admin commands (core only; wiring is stage 7b) -------------------------------------------


async def test_admin_cancel_of_an_assigned_order(ctx: dict[str, Any], world: World) -> None:
    order_id = await world.order(status="assigned", courier="Motero0")
    async with ctx["session_factory"]() as session, session.begin():
        result = await cancellation.cancel_by_admin(
            session, ctx["settings"], code="YV-0001", admin_jid=ADMIN_JIDS[0], request_key="k"
        )
    assert result.outcome == "cancelled_assigned"
    rows = await outbox(ctx["session_factory"], result.outbound_ids)
    assert messages(rows)[1:] == [
        ("courier_order_cancelled", world.jid("Motero0"), COURIER_CANCELLED),
        (
            "admin_command_ok",
            ADMIN_JIDS[0],
            "✅ Listo: *YV-0001* cancelado y eliminado del grupo. Le avisé a Motero0.",
        ),
        (
            "store_order_cancelled_by_admin",
            STORE_JID,
            "❌ La administración canceló tu pedido *YV-0001*.\nYa le avisé a Motero0.",
        ),
    ]
    assert (await get_order(ctx["session_factory"], order_id)).cancelled_by == "admin"


async def test_admin_release(ctx: dict[str, Any], world: World) -> None:
    order_id = await world.order(status="assigned", courier="Motero0")
    async with ctx["session_factory"]() as session, session.begin():
        result = await claims.release_by_admin(
            session, ctx["settings"], code="YV-0001", admin_jid=ADMIN_JIDS[0], request_key="k"
        )
    assert result.outcome == "released"
    rows = messages(await outbox(ctx["session_factory"], result.outbound_ids))
    assert rows[:2] == released_messages(world, "Motero0")[:2]
    assert rows[2:] == [
        (
            "courier_order_released_by_admin",
            world.jid("Motero0"),
            "La administración liberó el pedido *YV-0001*; ya no está a tu nombre.",
        ),
        ("admin_command_ok", ADMIN_JIDS[0], "✅ Listo: *YV-0001* liberado."),
    ]
    order = await get_order(ctx["session_factory"], order_id)
    assert (order.status, order.courier_id) == ("published", None)


@pytest.mark.parametrize(
    ("status", "fn", "reason"),
    [
        (None, "cancel", "no existe un pedido con ese código"),
        ("cancelled", "cancel", "ya está cancelado"),
        ("published", "release", "no está asignado a ningún motero"),
        ("cancelled", "release", "ya está cancelado"),
    ],
)
async def test_admin_command_errors(
    ctx: dict[str, Any], world: World, status: str | None, fn: str, reason: str
) -> None:
    if status is not None:
        await world.order(status=status)
    call = cancellation.cancel_by_admin if fn == "cancel" else claims.release_by_admin
    async with ctx["session_factory"]() as session, session.begin():
        result = await call(
            session, ctx["settings"], code="YV-0001", admin_jid=ADMIN_JIDS[0], request_key="k"
        )
    assert result.outcome.startswith("error:")
    verb = "cancelar" if fn == "cancel" else "liberar"
    assert messages(await outbox(ctx["session_factory"], result.outbound_ids)) == [
        ("admin_command_error", ADMIN_JIDS[0], f"No pude {verb} *YV-0001*: {reason}.")
    ]


# --- publication (T06, T07) and the group-post hooks --------------------------------------------

POST_BODY = f"🆕 *Pedido YV-0001*\n\n{DETAILS}\n\n👉 *Reaccioná a este mensaje para tomarlo.*"
PUBLISHED = (
    "✅ Pedido *YV-0001* enviado a los moteros. Te aviso cuando uno lo tome.\n\n"
    "Para cancelarlo, respondé este mensaje con *cancelar*."
)
SUMMARY = (
    f"📝 Revisá tu pedido:\n\n{DETAILS}\n\n"
    "¿Confirmás el pedido? Respondé *SÍ* para enviarlo o corregí lo que haga falta."
)


async def awaiting(world: World, engine: AsyncEngine) -> int:
    return await f.insert_ok(
        engine,
        f.order(
            world.store_id,
            status="awaiting_confirmation",
            business_date=D,
            pickup_address="casa nogal almendros",
            delivery_address="el ceibo 2871",
            product="almacen",
            payment_method="transfer",
            payment_method_raw="transferencia",
            confirmation_requested_at=at(9, 14),
            draft_expires_at=at(9, 16),
        ),
    )


async def test_confirmation_publishes_and_the_sent_post_enables_claims(
    ctx: dict[str, Any], world: World, engine: AsyncEngine, clock: Clock
) -> None:
    order_id = await awaiting(world, engine)
    result = await store_says(ctx, world, "sí", wa_message_id="WA-SI")

    assert (result.outcome, result.order_id) == ("published", order_id)
    order = await get_order(ctx["session_factory"], order_id)
    assert (order.status, order.code, order.confirmed_at, order.published_at) == (
        "published",
        "YV-0001",
        at(9, 15),
        at(9, 15),
    )
    assert (order.group_message_id, order.draft_expires_at) == (None, None)
    rows = await outbox(ctx["session_factory"], result.outbound_ids)
    assert messages(rows) == [
        ("group_order_post", GROUP, POST_BODY),
        ("store_order_published", STORE_JID, PUBLISHED),
    ]
    assert [e[0] for e in await events(ctx["session_factory"], order_id)] == [
        "confirmed",
        "published",
    ]
    clock.now = at(9, 15, 2)
    assert await deliver(ctx) == ["sent", "sent"]
    order = await get_order(ctx["session_factory"], order_id)
    post = (await outbox(ctx["session_factory"], result.outbound_ids))[0]
    assert order.group_message_id == post.wa_message_id
    assert order.unclaimed_alert_at == at(9, 15, 22)  # CU-03 4a: from the real post
    claimed = await react(world, ctx, "Motero0", post=post.wa_message_id)
    assert claimed.outcome == "claimed"

    again = await store_says(ctx, world, "sí", wa_message_id="WA-SI-2")
    assert (again.outcome, again.outbound_ids) == ("ignored:nothing_to_confirm", [])


async def test_confirmation_outside_hours_does_not_publish(
    ctx: dict[str, Any], world: World, engine: AsyncEngine, clock: Clock
) -> None:
    order_id = await awaiting(world, engine)
    clock.now = at(9, 23, 30)
    result = await store_says(ctx, world, "sí", wa_message_id="WA-SI")
    assert result.outcome == "not_sent_closed"
    assert messages(await outbox(ctx["session_factory"], result.outbound_ids)) == [
        (
            "store_order_not_sent_closed",
            STORE_JID,
            "🚫 No envié tu pedido porque en este momento no estamos tomando pedidos "
            "(horario de hoy: 09:00 a 23:00). Mandalo de nuevo cuando volvamos a tomar pedidos.",
        )
    ]
    order = await get_order(ctx["session_factory"], order_id)
    assert (order.status, order.code) == ("awaiting_confirmation", None)


async def test_confirmation_during_a_closure_names_the_reason(
    ctx: dict[str, Any], world: World, engine: AsyncEngine
) -> None:
    await awaiting(world, engine)
    await f.insert_ok(
        engine,
        f.override(status="active", reason="lluvia", starts_at=at(9, 10), confirmed_at=at(9, 10)),
    )
    result = await store_says(ctx, world, "sí")
    assert messages(await outbox(ctx["session_factory"], result.outbound_ids))[0][2] == (
        "🚫 No envié tu pedido porque en este momento no estamos tomando pedidos por lluvia. "
        "Mandalo de nuevo cuando volvamos a tomar pedidos."
    )


async def test_yes_quoting_an_older_summary_resends_the_current_one(
    ctx: dict[str, Any], world: World, engine: AsyncEngine
) -> None:
    order_id = await awaiting(world, engine)
    old_summary = SUMMARY.replace("el ceibo 2871", "el ceibo 2800")  # before a correction
    for wa, body in (("OLD-SUMMARY", old_summary), ("NEW-SUMMARY", SUMMARY)):
        await f.insert_ok(
            engine,
            f.outbound(
                template_key="store_order_confirm_request",
                recipient_jid=STORE_JID,
                order_id=order_id,
                status="sent",
                wa_message_id=wa,
                body=body,
            ),
        )
    result = await store_says(ctx, world, "sí", quoted_wa_message_id="OLD-SUMMARY")
    assert result.outcome == "summary_resent"
    assert messages(await outbox(ctx["session_factory"], result.outbound_ids)) == [
        ("store_order_confirm_request", STORE_JID, SUMMARY)
    ]
    assert (await get_order(ctx["session_factory"], order_id)).status == "awaiting_confirmation"

    current = await store_says(ctx, world, "sí", quoted_wa_message_id="NEW-SUMMARY")
    assert current.outcome == "published"


async def test_no_discards_the_order_awaiting_confirmation(
    ctx: dict[str, Any], world: World, engine: AsyncEngine
) -> None:
    order_id = await awaiting(world, engine)
    result = await store_says(ctx, world, "no")
    assert result.outcome == "draft_cancelled"
    assert messages(await outbox(ctx["session_factory"], result.outbound_ids)) == [
        (
            "store_draft_cancelled",
            STORE_JID,
            "Listo, descarté el pedido; no se envió a los moteros.",
        )
    ]
    order = await get_order(ctx["session_factory"], order_id)
    assert (order.status, order.cancelled_by, order.cancelled_at) == (
        "cancelled",
        "store",
        at(9, 15),
    )


async def test_a_post_sent_after_the_cancel_is_deleted_right_away(
    ctx: dict[str, Any], world: World, engine: AsyncEngine
) -> None:
    order_id = await awaiting(world, engine)
    published = await store_says(ctx, world, "sí")
    cancelled = await store_says(ctx, world, "cancelar")  # the post is still in the outbox
    assert cancelled.outcome == "cancelled"
    assert [
        r.template_key for r in await outbox(ctx["session_factory"], cancelled.outbound_ids)
    ] == ["store_order_cancelled"]

    post_id = published.outbound_ids[0]
    assert await send_outbound(ctx, post_id) == "sent"
    rows = await outbox(ctx["session_factory"])
    post = next(r for r in rows if r.id == post_id)
    delete = [r for r in rows if r.template_key == "group_order_delete"]
    assert [(r.target_wa_message_id, r.status) for r in delete] == [(post.wa_message_id, "pending")]
    assert (delete[0].id,) in [a[1] for a in await queued(ctx["redis"], SEND_OUTBOUND)]

    assert await send_outbound(ctx, delete[0].id) == "sent"
    order = await get_order(ctx["session_factory"], order_id)
    assert order.group_post_deleted_at is not None
    assert [e[0] for e in await events(ctx["session_factory"], order_id)][-1] == (
        "group_post_deleted"
    )


async def test_a_failed_delete_is_recorded(
    ctx: dict[str, Any], world: World, evo: respx.MockRouter
) -> None:
    order_id = await world.order()
    cancelled = await store_says(ctx, world, "cancelar")
    evo.routes["delete"].mock(return_value=httpx.Response(400, json={"error": "nope"}))
    delete_id = cancelled.outbound_ids[0]
    assert await send_outbound(ctx, delete_id) == "failed"
    evs = await events(ctx["session_factory"], order_id)
    assert evs[-1][0] == "group_post_delete_failed"
    assert (await get_order(ctx["session_factory"], order_id)).group_post_deleted_at is None


# --- store "cancelar" resolution (CU-09) ----------------------------------------------------


async def test_cancel_by_code_and_by_quote(
    ctx: dict[str, Any], world: World, engine: AsyncEngine
) -> None:
    first = await world.order(code="YV-0001", group_message_id="P1")
    second = await world.order(code="YV-0002", group_message_id="P2")
    await f.insert_ok(
        engine,
        f.outbound(
            template_key="store_order_published",
            recipient_jid=STORE_JID,
            order_id=second,
            status="sent",
            wa_message_id="PUBLISHED-2",
        ),
    )
    by_code = await store_says(ctx, world, "cancelar yv-0001")
    assert (by_code.outcome, by_code.order_id) == ("cancelled", first)
    by_quote = await store_says(ctx, world, "cancelar", quoted_wa_message_id="PUBLISHED-2")
    assert (by_quote.outcome, by_quote.order_id) == ("cancelled", second)


async def test_cancel_without_target_and_several_active_orders_asks(
    ctx: dict[str, Any], world: World
) -> None:
    await world.order(code="YV-0001", group_message_id="P1")
    await world.order(code="YV-0002", group_message_id="P2", status="assigned", courier="Motero0")
    result = await store_says(ctx, world, "cancelar")
    assert result.outcome == "ambiguous"
    assert messages(await outbox(ctx["session_factory"], result.outbound_ids)) == [
        (
            "store_cancel_ambiguous",
            STORE_JID,
            "Tenés varios pedidos activos: YV-0001, YV-0002. Respondé *cancelar* citando el "
            "mensaje del pedido o escribí *cancelar* y el código, por ejemplo *cancelar YV-0001*.",
        )
    ]


async def test_cancel_with_an_unknown_code(ctx: dict[str, Any], world: World) -> None:
    await world.order(code="YV-0001")
    result = await store_says(ctx, world, "cancelar YV-9999")
    assert result.outcome == "not_found"
    assert messages(await outbox(ctx["session_factory"], result.outbound_ids)) == [
        (
            "store_order_not_found",
            STORE_JID,
            "No encontré un pedido activo con ese código. Tus pedidos activos: YV-0001.",
        )
    ]


async def test_cancel_with_nothing_active(ctx: dict[str, Any], world: World) -> None:
    result = await store_says(ctx, world, "cancelar")
    assert messages(await outbox(ctx["session_factory"], result.outbound_ids)) == [
        ("store_cancel_nothing_active", STORE_JID, "No tenés pedidos activos para cancelar.")
    ]
    unknown = await store_says(ctx, world, "cancelar YV-0404")
    assert messages(await outbox(ctx["session_factory"], unknown.outbound_ids))[0][2] == (
        "No encontré un pedido activo con ese código. Tus pedidos activos: ninguno."
    )


async def test_cancel_of_a_cancelled_order_is_not_allowed(
    ctx: dict[str, Any], world: World
) -> None:
    await world.order(status="cancelled")
    first = await store_says(ctx, world, "cancelar YV-0001")
    assert first.outcome == "not_allowed:cancelled"
    assert messages(await outbox(ctx["session_factory"], first.outbound_ids)) == [
        (
            "store_cancel_not_allowed",
            STORE_JID,
            "El pedido *YV-0001* ya está cancelado y no se puede cancelar.",
        )
    ]


async def test_another_stores_code_is_not_found(
    ctx: dict[str, Any], world: World, engine: AsyncEngine
) -> None:
    other = await f.insert_ok(engine, f.store(display_name="Otro"))
    await f.insert_ok(engine, f.published(other, code="YV-0077"))
    result = await store_says(ctx, world, "cancelar YV-0077")
    assert result.outcome == "not_found"


async def test_concurrent_store_cancels_cancel_once(ctx: dict[str, Any], world: World) -> None:
    order_id = await world.order(status="assigned", courier="Motero0")
    a, b = await asyncio.gather(
        store_says(ctx, world, "cancelar"), store_says(ctx, world, "cancelar YV-0001")
    )
    assert sorted([a.outcome, b.outcome]) == ["cancelled_assigned", "not_allowed:cancelled"]
    templates = [r.template_key for r in await outbox(ctx["session_factory"])]
    assert templates.count("courier_order_cancelled") == 1
    assert [e[0] for e in await events(ctx["session_factory"], order_id)] == ["cancelled"]


# --- unclaimed alert (T15) ------------------------------------------------------------------


async def test_unclaimed_alert_once_per_publication_period(
    ctx: dict[str, Any], world: World, clock: Clock
) -> None:
    order_id = await world.order()  # published 14:30, unclaimed_alert_at 14:50
    assert await order_jobs.alert_unclaimed_orders(ctx) == 2
    assert await order_jobs.alert_unclaimed_orders(ctx) == 0
    rows = await outbox(ctx["session_factory"])
    assert messages(rows) == [
        (
            "admin_order_unclaimed",
            jid,
            f"⚠️ El pedido *YV-0001* lleva 20 minutos sin motero.\n\n{DETAILS}\n\n"
            "Sigue publicado. Si querés cancelarlo, escribí *cancelar YV-0001*.",
        )
        for jid in ADMIN_JIDS
    ]
    order = await get_order(ctx["session_factory"], order_id)
    assert order.unclaimed_alert_sent_at == at(9, 15)

    # A claim and a release start a new period: one more alert after it elapses.
    await react(world, ctx, "Motero0")
    await unreact(world, ctx, "Motero0")
    assert await order_jobs.alert_unclaimed_orders(ctx) == 0
    clock.now = at(9, 15, 21)
    assert await order_jobs.alert_unclaimed_orders(ctx) == 2


async def test_unclaimed_alert_skips_assigned_and_cancelled(
    ctx: dict[str, Any], world: World
) -> None:
    await world.order(status="assigned", courier="Motero0", code="YV-0001", group_message_id="P1")
    await world.order(status="cancelled", code="YV-0002", group_message_id="P2")
    assert await order_jobs.alert_unclaimed_orders(ctx) == 0


# --- reactions before the post id is recorded (D-19, ya_voy.orders.parking) ------------------

NEW_POST = "POST-FROM-EVOLUTION"


async def pending_post(world: World, engine: AsyncEngine) -> tuple[int, int]:
    """A published order whose group post is still in the outbox (no ``group_message_id``)."""
    order_id = await world.order(group_message_id=None)
    post_id = await f.insert_ok(
        engine,
        f.outbound(
            template_key="group_order_post",
            recipient_jid=GROUP,
            order_id=order_id,
            body=POST_BODY,
        ),
    )
    return order_id, post_id


class Gate:
    """Evolution's sendText for the post: answers with ``NEW_POST``, optionally only after the
    test opens the gate (the post is "accepted" but ``send_outbound`` has not committed)."""

    def __init__(self, evo: respx.MockRouter, *, hold: bool) -> None:
        self.called = asyncio.Event()
        self.release = asyncio.Event()
        if not hold:
            self.release.set()
        evo.routes["sendText"].mock(side_effect=self.respond)

    async def respond(self, request: httpx.Request) -> httpx.Response:
        self.called.set()
        await self.release.wait()
        return httpx.Response(201, json={"key": {"id": NEW_POST, "fromMe": True}})


def ts(second: int) -> dt.datetime:
    return at(9, 15) + dt.timedelta(seconds=second)


def on_new(second: int, inbound_event_id: int | None = None) -> dict[str, Any]:
    """Event fields of a reaction on ``NEW_POST`` sent at ``ts(second)``."""
    return {"post": NEW_POST, "reacted_at": ts(second), "inbound_event_id": inbound_event_id}


async def parked_rows(sf: SF) -> list[tuple[int, str, int]]:
    async with sf() as session:
        rows = await session.scalars(select(m.ParkedReaction).order_by(m.ParkedReaction.id))
        return [(r.courier_id, r.status, r.attempts) for r in rows]


async def retries(ctx: dict[str, Any]) -> list[tuple]:
    return [args for _, args in await queued(ctx["redis"], "retry_parked_reaction")]


async def test_reaction_processed_before_the_post_id_commits_wins(
    ctx: dict[str, Any], world: World, engine: AsyncEngine, evo: respx.MockRouter
) -> None:
    """Evolution accepted the post but ``send_outbound`` has not committed ``group_message_id``:
    the first reaction is parked, the hook applies it when the id commits, and a later reaction
    (processed after the commit) loses."""
    order_id, post_id = await pending_post(world, engine)
    gate = Gate(evo, hold=True)
    sending = asyncio.create_task(send_outbound(ctx, post_id))
    await asyncio.wait_for(gate.called.wait(), 5)

    try:
        first = await react(world, ctx, "Motero0", post=NEW_POST, reacted_at=ts(1))
        before_commit = (await get_order(ctx["session_factory"], order_id)).courier_id
    finally:
        gate.release.set()  # never leave send_outbound waiting on Evolution
        sent = await sending
    assert (first.outcome, first.order_id, first.outbound_ids) == ("parked", None, [])
    assert first.parked_id is not None
    assert await retries(ctx) == [(first.parked_id,)]
    assert before_commit is None
    assert sent == "sent"
    order = await get_order(ctx["session_factory"], order_id)
    assert (order.group_message_id, order.status, order.courier_id) == (
        NEW_POST,
        "assigned",
        world.couriers["Motero0"],
    )
    claim_rows = [r for r in await outbox(ctx["session_factory"]) if r.id != post_id]
    assert messages(claim_rows) == assigned_messages(world, "Motero0")
    assert claim_rows[2].quoted_wa_message_id == NEW_POST
    queued_ids = {a[0] for _, a in await queued(ctx["redis"], SEND_OUTBOUND)}
    assert {r.id for r in claim_rows} <= queued_ids  # enqueued after send_outbound's commit

    later = await react(world, ctx, "Motero1", post=NEW_POST, reacted_at=ts(2))
    assert later.outcome == "rejected:already_taken"

    # The deferred retry finds the reaction already applied: nothing more.
    assert await order_jobs.retry_parked_reaction(ctx, first.parked_id) == "ignored:applied"
    assert await parked_rows(ctx["session_factory"]) == [(world.couriers["Motero0"], "applied", 0)]
    assert [e[0] for e in await events(ctx["session_factory"], order_id)] == [
        "claimed",
        "claim_rejected",
    ]


async def test_parked_reactions_apply_in_whatsapp_order(
    ctx: dict[str, Any], world: World, engine: AsyncEngine, evo: respx.MockRouter
) -> None:
    """Processed in the opposite order of their ``messageTimestamp``: the earlier one wins.
    Equal timestamps fall back to arrival order (``inbound_event_id``)."""
    order_id, post_id = await pending_post(world, engine)
    Gate(evo, hold=False)
    late_event, early_event = await world.inbound(), await world.inbound()
    assert (await react(world, ctx, "Motero0", **on_new(5, late_event))).outcome == "parked"
    assert (await react(world, ctx, "Motero1", **on_new(3, early_event))).outcome == "parked"
    assert (await react(world, ctx, "Motero2", post=NEW_POST, reacted_at=ts(3))).outcome == (
        "parked"
    )

    assert await send_outbound(ctx, post_id) == "sent"

    order = await get_order(ctx["session_factory"], order_id)
    assert (order.status, order.courier_id) == ("assigned", world.couriers["Motero1"])
    evs = await events(ctx["session_factory"], order_id)
    assert evs == [
        ("claimed", world.couriers["Motero1"]),
        ("claim_rejected", world.couriers["Motero2"]),  # same timestamp, no inbound id: after
        ("claim_rejected", world.couriers["Motero0"]),
    ]
    assert [s for _, s, _ in await parked_rows(ctx["session_factory"])] == ["applied"] * 3


async def test_a_reaction_arriving_while_the_hook_records_the_id_waits_for_it(
    ctx: dict[str, Any],
    world: World,
    engine: AsyncEngine,
    evo: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Overtaking: a reaction parked before the id was known and a later one that arrives while
    the hook is recording the id. The later one waits on the target's advisory lock, then sees
    the id; the hook has already applied the parked one, so the earlier reaction wins."""
    order_id, post_id = await pending_post(world, engine)
    Gate(evo, hold=False)
    first = await react(world, ctx, "Motero0", post=NEW_POST, reacted_at=ts(1))
    assert first.outcome == "parked"

    in_hook, resume = asyncio.Event(), asyncio.Event()
    original = parking.apply_parked

    async def paused(*args: Any, **kw: Any) -> Any:
        in_hook.set()
        await resume.wait()
        return await original(*args, **kw)

    monkeypatch.setattr(parking, "apply_parked", paused)
    sending = asyncio.create_task(send_outbound(ctx, post_id))
    await asyncio.wait_for(in_hook.wait(), 5)
    monkeypatch.setattr(parking, "apply_parked", original)

    later = asyncio.create_task(react(world, ctx, "Motero1", post=NEW_POST, reacted_at=ts(2)))
    try:
        await asyncio.sleep(0.3)
        blocked = not later.done()  # waiting on the advisory lock the hook holds
    finally:
        resume.set()  # never leave send_outbound paused (it holds row locks)
        sent = await sending
        outcome = (await later).outcome
    assert blocked
    assert (sent, outcome) == ("sent", "rejected:already_taken")
    order = await get_order(ctx["session_factory"], order_id)
    assert order.courier_id == world.couriers["Motero0"]


@pytest.mark.parametrize("attempt", range(5))
async def test_reactions_racing_the_post_commit_first_parked_wins(
    ctx: dict[str, Any], world: World, engine: AsyncEngine, evo: respx.MockRouter, attempt: int
) -> None:
    """Reactions racing the post's commit: whatever the interleaving, exactly one assignment,
    and if a reaction was parked the earliest parked one (by timestamp) is the winner."""
    order_id, post_id = await pending_post(world, engine)
    gate = Gate(evo, hold=True)
    sending = asyncio.create_task(send_outbound(ctx, post_id))
    await asyncio.wait_for(gate.called.wait(), 5)
    early = asyncio.create_task(react(world, ctx, "Motero0", post=NEW_POST, reacted_at=ts(1)))
    try:
        await asyncio.sleep(0.01 * attempt)
    finally:
        gate.release.set()
    others = [
        asyncio.create_task(react(world, ctx, n, post=NEW_POST, reacted_at=ts(2 + i)))
        for i, n in enumerate(("Motero1", "Motero2"))
    ]
    results = [await early, *[await t for t in others]]
    assert await sending == "sent"
    order = await get_order(ctx["session_factory"], order_id)
    assert order.status == "assigned"
    claims_ = [e for e in await events(ctx["session_factory"], order_id) if e[0] == "claimed"]
    assert len(claims_) == 1
    if results[0].outcome == "parked":
        assert order.courier_id == world.couriers["Motero0"]
    assert sum(r.outcome == "claimed" for r in results) <= 1


async def test_a_reaction_on_an_unknown_message_is_discarded_after_the_last_retry(
    ctx: dict[str, Any], world: World, caplog: pytest.LogCaptureFixture
) -> None:
    result = await react(world, ctx, "Motero0", post="NEVER-AN-ORDER-POST")
    assert result.outcome == "parked"
    parked_id = result.parked_id
    assert await retries(ctx) == [(parked_id,)]

    outcomes = [await order_jobs.retry_parked_reaction(ctx, parked_id) for _ in range(3)]
    assert outcomes == ["parked", "parked", "discarded"]
    assert await retries(ctx) == [(parked_id,)] * 3  # the first one plus two re-enqueues
    assert await parked_rows(ctx["session_factory"]) == [
        (world.couriers["Motero0"], "discarded", 3)
    ]
    assert any("parked reaction discarded" in r.getMessage() for r in caplog.records)
    assert await order_jobs.retry_parked_reaction(ctx, parked_id) == "ignored:discarded"
    assert await outbox(ctx["session_factory"]) == []


async def test_a_retry_applies_the_reaction_once_the_post_is_known(
    ctx: dict[str, Any], world: World, engine: AsyncEngine
) -> None:
    """Fallback path: the post id became known without the hook applying the parked reaction."""
    order_id = await world.order(group_message_id=None)
    result = await react(world, ctx, "Motero0", post="LATE-POST")
    async with engine.begin() as conn:
        await conn.execute(
            update(m.Order).where(m.Order.id == order_id).values(group_message_id="LATE-POST")
        )
    assert await order_jobs.retry_parked_reaction(ctx, result.parked_id) == "applied"
    order = await get_order(ctx["session_factory"], order_id)
    assert order.courier_id == world.couriers["Motero0"]
    assert len(await outbox(ctx["session_factory"])) == 3


async def test_parked_removal_and_duplicates(
    ctx: dict[str, Any], world: World, engine: AsyncEngine, evo: respx.MockRouter
) -> None:
    """A removal waits like an addition and is applied after it (timestamp order); a redelivered
    parked event is parked once and gets a single chain of retries."""
    order_id, post_id = await pending_post(world, engine)
    Gate(evo, hold=False)
    added, removed = await world.inbound(), await world.inbound()
    first = await react(world, ctx, "Motero0", **on_new(1, added))
    again = await react(world, ctx, "Motero0", **on_new(1, added))
    assert (first.outcome, again.outcome, again.parked_id) == ("parked", "parked", None)
    gone = await unreact(world, ctx, "Motero0", **on_new(4, removed))
    assert gone.outcome == "parked"
    assert len(await retries(ctx)) == 2

    assert await send_outbound(ctx, post_id) == "sent"
    order = await get_order(ctx["session_factory"], order_id)
    assert (order.status, order.courier_id) == ("published", None)
    assert [e[0] for e in await events(ctx["session_factory"], order_id)] == [
        "claimed",
        "released_by_courier",
    ]
    # Redelivered once more after the id is known: duplicate of what the hook applied.
    assert (await react(world, ctx, "Motero0", **on_new(1, added))).outcome == "duplicate"
    assert await order_jobs.retry_parked_reaction(ctx, gone.parked_id) == "ignored:applied"
