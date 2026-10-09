"""Recovery sweeps re-enqueue work whose arq job was lost (docs/06 cron table)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

from arq.connections import ArqRedis
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration import factories as f
from tests.integration.conftest import queued
from ya_voy.db.models import InboundEvent, OutboundMessage
from ya_voy.worker.sweeps import recover_inbound_events, recover_outbound_messages

Ctx = Callable[..., dict[str, Any]]


async def _age(engine: AsyncEngine, model: Any, column: Any, row_id: int, seconds: int) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            update(model)
            .where(model.id == row_id)
            .values({column: func.now() - timedelta(seconds=seconds)})
        )


async def test_recover_inbound_events_only_old_received(
    engine: AsyncEngine, worker_ctx: Ctx, arq_redis: ArqRedis
) -> None:
    old = await f.insert_ok(engine, f.inbound())
    fresh = await f.insert_ok(engine, f.inbound())
    old_done = await f.insert_ok(engine, f.inbound(status="processed"))
    old_failed = await f.insert_ok(engine, f.inbound(status="failed"))
    for row_id in (old, old_done, old_failed):
        await _age(engine, InboundEvent, "received_at", row_id, 120)

    ctx = worker_ctx(inbound_recovery_seconds=60)
    assert await recover_inbound_events(ctx) == 1
    assert await queued(arq_redis) == [("process_inbound_event", (old,))]
    assert fresh not in {a for (_, (a,)) in await queued(arq_redis)}


async def test_recover_outbound_messages_only_old_pending(
    engine: AsyncEngine, worker_ctx: Ctx, arq_redis: ArqRedis
) -> None:
    old = await f.insert_ok(engine, f.outbound())
    await f.insert_ok(engine, f.outbound())  # fresh
    old_sent = await f.insert_ok(engine, f.outbound(status="sent"))
    old_failed = await f.insert_ok(engine, f.outbound(status="failed"))
    for row_id in (old, old_sent, old_failed):
        await _age(engine, OutboundMessage, "created_at", row_id, 120)

    assert await recover_outbound_messages(worker_ctx(inbound_recovery_seconds=60)) == 1
    assert await queued(arq_redis) == [("send_outbound", (old,))]


async def test_sweeps_with_nothing_to_do(worker_ctx: Ctx, arq_redis: ArqRedis) -> None:
    ctx = worker_ctx()
    assert await recover_inbound_events(ctx) == 0
    assert await recover_outbound_messages(ctx) == 0
    assert await queued(arq_redis) == []
