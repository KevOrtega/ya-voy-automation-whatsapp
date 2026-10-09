"""Manual reprocessing of failed inbound events (docs/06)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from scripts import reprocess_inbound
from tests.integration import factories as f
from tests.integration.conftest import queued, redis_test_url
from ya_voy.db.models import InboundEvent
from ya_voy.queue import PROCESS_INBOUND_EVENT
from ya_voy.worker.reprocess import reset_failed_inbound_events

Ctx = Callable[..., dict[str, Any]]


async def make_event(engine: AsyncEngine, status: str, **kw: Any) -> int:
    return await f.insert_ok(engine, f.inbound(status=status, **kw))


async def test_resets_only_failed_events(worker_ctx: Ctx, engine: AsyncEngine) -> None:
    ctx = worker_ctx()
    failed = await make_event(engine, "failed", error="boom", attempts=1)
    processed = await make_event(engine, "processed")

    async with ctx["session_factory"]() as session, session.begin():
        reset = await reset_failed_inbound_events(session, ids=[failed, processed])
    assert reset == [failed]

    async with ctx["session_factory"]() as session:
        row = await session.get(InboundEvent, failed)
        other = await session.get(InboundEvent, processed)
    assert row is not None and other is not None
    assert (row.status, row.error, row.processed_at, row.attempts) == ("received", None, None, 1)
    assert other.status == "processed"

    # Idempotent: running it again resets nothing.
    async with ctx["session_factory"]() as session, session.begin():
        assert await reset_failed_inbound_events(session, ids=[failed]) == []


async def test_all_failed(worker_ctx: Ctx, engine: AsyncEngine) -> None:
    ctx = worker_ctx()
    ids = [await make_event(engine, "failed", error="x") for _ in range(2)]
    async with ctx["session_factory"]() as session, session.begin():
        reset = await reset_failed_inbound_events(session, all_failed=True)
    assert set(ids) <= set(reset)


async def test_requires_ids_or_all(worker_ctx: Ctx) -> None:
    ctx = worker_ctx()
    async with ctx["session_factory"]() as session:
        with pytest.raises(ValueError):
            await reset_failed_inbound_events(session)
        with pytest.raises(ValueError):
            await reset_failed_inbound_events(session, ids=[1], all_failed=True)


async def test_script_reenqueues_the_events(
    worker_ctx: Ctx, engine: AsyncEngine, database_url: str
) -> None:
    ctx = worker_ctx()
    failed = await make_event(engine, "failed", error="boom")
    settings = ctx["settings"].model_copy(
        update={"database_url": database_url, "redis_url": redis_test_url()}
    )

    reset = await reprocess_inbound.run(settings, [failed], all_failed=False)

    assert reset == [failed]
    assert (PROCESS_INBOUND_EVENT, (failed,)) in await queued(ctx["redis"], PROCESS_INBOUND_EVENT)


def test_script_rejects_ids_with_all() -> None:
    with pytest.raises(SystemExit):
        reprocess_inbound.main(["1", "--all"])
    with pytest.raises(SystemExit):
        reprocess_inbound.main([])
