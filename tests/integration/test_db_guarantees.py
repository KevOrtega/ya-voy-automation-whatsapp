"""Database-level guarantees under concurrency (concurrency-and-race-testing skill).

Each actor uses its own session/connection and commits, as real workers would.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tests.integration import factories as f
from ya_voy.db import models as m
from ya_voy.db.queries import enqueue_outbound_message, insert_inbound_event, next_order_code

N_ACTORS = 12
Sessions = async_sessionmaker[AsyncSession]

# T10 from docs/03-estados-pedido.md, verbatim (the claim helper itself is stage 5).
CLAIM_SQL = text(
    """
    UPDATE orders
       SET courier_id = :courier_id, last_courier_id = :courier_id,
           status = 'assigned', assigned_at = now(), updated_at = now()
     WHERE id = :order_id
       AND courier_id IS NULL
       AND status = 'published'
    RETURNING id
    """
)


async def run_together(n: int, actor: Callable[[int], Awaitable[Any]]) -> list[Any]:
    """Start ``n`` actors at the same moment; return their results (exceptions included)."""
    start = asyncio.Event()

    async def wrapped(i: int) -> Any:
        await start.wait()
        return await actor(i)

    tasks = [asyncio.create_task(wrapped(i)) for i in range(n)]
    await asyncio.sleep(0)
    start.set()
    return await asyncio.gather(*tasks, return_exceptions=True)


async def wait_until_blocked(engine: AsyncEngine, task: asyncio.Task[Any]) -> None:
    """Assert ``task`` is waiting on a row/index lock held by another transaction."""
    for _ in range(100):
        async with engine.connect() as conn:
            waiting = await conn.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            )
        if waiting:
            assert not task.done()
            return
        await asyncio.sleep(0.02)
    pytest.fail("the second actor never blocked on a lock")


# --- inbound dedup (RF-59, CU-11) ---------------------------------------------------------


async def _insert_inbound(sessions: Sessions, wa_message_id: str) -> int | None:
    async with sessions() as session, session.begin():
        return await insert_inbound_event(
            session,
            wa_message_id=wa_message_id,
            instance="yavoy",
            event_type="messages.upsert",
            payload={"key": {"id": wa_message_id}},
            remote_jid="5491155551234@s.whatsapp.net",
        )


async def test_inbound_dedup_sequential_rf59(session_factory: Sessions) -> None:
    first = await _insert_inbound(session_factory, "3EB0DUP")
    second = await _insert_inbound(session_factory, "3EB0DUP")
    assert isinstance(first, int)
    assert second is None
    other = await _insert_inbound(session_factory, "3EB0OTHER")
    assert other is not None and other != first


async def test_inbound_dedup_concurrent_rf59(
    engine: AsyncEngine, session_factory: Sessions
) -> None:
    results = await run_together(N_ACTORS, lambda _: _insert_inbound(session_factory, "3EB0RACE"))
    assert not [r for r in results if isinstance(r, BaseException)]
    assert len([r for r in results if r is not None]) == 1  # exactly one enqueue
    async with engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(m.InboundEvent)) == 1


# --- outbox idempotency (RF-60, D-03) -----------------------------------------------------


async def _enqueue(sessions: Sessions, key: str, body: str = "hola") -> int | None:
    async with sessions() as session, session.begin():
        return await enqueue_outbound_message(
            session,
            idempotency_key=key,
            template_key="courier_order_assigned",
            recipient_jid="5491100000007@s.whatsapp.net",
            body=body,
        )


async def test_outbox_idempotency_sequential_rf60(
    engine: AsyncEngine, session_factory: Sessions
) -> None:
    key = "order:42:claimed:courier:7"
    first = await _enqueue(session_factory, key, body="first")
    assert isinstance(first, int)
    assert await _enqueue(session_factory, key, body="second") is None
    async with engine.connect() as conn:
        rows = (await conn.execute(select(m.OutboundMessage.body))).scalars().all()
    assert rows == ["first"]  # the duplicate changed nothing


async def test_outbox_idempotency_concurrent_rf60(
    engine: AsyncEngine, session_factory: Sessions
) -> None:
    results = await run_together(
        N_ACTORS, lambda _: _enqueue(session_factory, "order:42:delete:3EB0AAA")
    )
    assert not [r for r in results if isinstance(r, BaseException)]
    assert len([r for r in results if r is not None]) == 1
    async with engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(m.OutboundMessage)) == 1


async def test_outbox_rolled_back_with_its_transaction(
    engine: AsyncEngine, session_factory: Sessions
) -> None:
    """Enqueued in the same transaction as the state change: a rollback leaves no message."""
    async with session_factory() as session, session.begin():
        await enqueue_outbound_message(
            session,
            idempotency_key="order:1:published",
            template_key="store_order_published",
            recipient_jid="x@s.whatsapp.net",
        )
        await session.rollback()
    async with engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(m.OutboundMessage)) == 0
    assert await _enqueue(session_factory, "order:1:published") is not None


# --- order codes (order_code_seq) ---------------------------------------------------------


async def test_order_codes_sequential(session_factory: Sessions) -> None:
    async with session_factory() as session:
        codes = [await next_order_code(session, "YV-") for _ in range(3)]
    assert codes == ["YV-0001", "YV-0002", "YV-0003"]


async def test_order_codes_unique_under_concurrency(session_factory: Sessions) -> None:
    async def take(_: int) -> str:
        async with session_factory() as session, session.begin():
            return await next_order_code(session, "YV-")

    codes = await run_together(N_ACTORS, take)
    assert len(set(codes)) == N_ACTORS
    assert all(isinstance(c, str) and c.startswith("YV-") for c in codes)


# --- one order in preparation per store (RN-09) -------------------------------------------


@pytest.mark.parametrize("first_outcome", ["commit", "rollback"])
async def test_concurrent_drafts_same_store_rn09(
    engine: AsyncEngine, session_factory: Sessions, first_outcome: str
) -> None:
    sid = await f.insert_ok(engine, f.store())
    async with session_factory() as a:
        await a.begin()
        await a.execute(f.order(sid))  # A holds the draft, uncommitted

        async def actor_b() -> None:
            async with session_factory() as b, b.begin():
                await b.execute(f.order(sid))

        task_b = asyncio.create_task(actor_b())
        await wait_until_blocked(engine, task_b)  # B waits on A's index entry
        await (a.commit() if first_outcome == "commit" else a.rollback())

    if first_outcome == "commit":
        with pytest.raises(IntegrityError, match="orders_one_draft_per_store"):
            await task_b
    else:
        await task_b
    async with engine.connect() as conn:
        drafts = await conn.scalar(
            select(func.count()).select_from(m.Order).where(m.Order.store_id == sid)
        )
    assert drafts == 1


async def test_draft_burst_same_store_single_draft_rn09(
    engine: AsyncEngine, session_factory: Sessions
) -> None:
    sid = await f.insert_ok(engine, f.store())

    async def create(_: int) -> None:
        async with session_factory() as session, session.begin():
            await session.execute(f.order(sid))

    results = await run_together(N_ACTORS, create)
    errors = [r for r in results if isinstance(r, BaseException)]
    assert len(errors) == N_ACTORS - 1
    assert all(isinstance(e, IntegrityError) for e in errors)
    async with engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(m.Order)) == 1


# --- single assignment per order (RN-02, D-02) --------------------------------------------


async def _published_order(engine: AsyncEngine) -> tuple[int, list[int]]:
    async with engine.begin() as conn:
        sid = await f.insert_row(conn, f.store())
        oid = await f.insert_row(conn, f.published(sid))
        couriers = [await f.insert_row(conn, f.courier()) for _ in range(N_ACTORS)]
    return oid, couriers


async def test_claim_race_single_winner_rn02(
    engine: AsyncEngine, session_factory: Sessions
) -> None:
    oid, couriers = await _published_order(engine)

    async def claim(i: int) -> int | None:
        async with session_factory() as session, session.begin():
            result = await session.execute(CLAIM_SQL, {"courier_id": couriers[i], "order_id": oid})
            return couriers[i] if result.scalar_one_or_none() is not None else None

    results = await run_together(N_ACTORS, claim)
    assert not [r for r in results if isinstance(r, BaseException)]
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    async with engine.connect() as conn:
        row = (await conn.execute(select(m.Order).where(m.Order.id == oid))).one()
    assert (row.status, row.courier_id, row.last_courier_id) == ("assigned", winners[0], winners[0])


async def test_claim_loser_blocks_then_gets_zero_rows_rn02(
    engine: AsyncEngine, session_factory: Sessions
) -> None:
    """Deterministic interleaving: B re-checks the WHERE after A commits (READ COMMITTED)."""
    oid, couriers = await _published_order(engine)
    async with session_factory() as a:
        await a.begin()
        won = await a.execute(CLAIM_SQL, {"courier_id": couriers[0], "order_id": oid})
        assert won.scalar_one_or_none() == oid

        async def actor_b() -> int | None:
            async with session_factory() as b, b.begin():
                res = await b.execute(CLAIM_SQL, {"courier_id": couriers[1], "order_id": oid})
                return res.scalar_one_or_none()

        task_b = asyncio.create_task(actor_b())
        await wait_until_blocked(engine, task_b)
        await a.commit()
    assert await task_b is None
    async with engine.connect() as conn:
        assert await conn.scalar(select(m.Order.courier_id).where(m.Order.id == oid)) == couriers[0]
