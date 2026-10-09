"""Webhook -> inbound_events -> arq queue against the real Postgres and Redis (RF-59, CU-11)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
from arq.connections import ArqRedis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.evolution_fixtures import COURIERS_GROUP_JID, INSTANCE, load_fixture
from tests.integration.conftest import queued, redis_test_url
from ya_voy.api.main import create_app
from ya_voy.config import Settings
from ya_voy.db.models import InboundEvent

SECRET = "integration-secret"
URL = f"/webhooks/evolution/{SECRET}"


@pytest.fixture
async def client(database_url: str, arq_redis: ArqRedis) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(
        _env_file=None,
        database_url=database_url,
        redis_url=redis_test_url(),
        webhook_secret=SECRET,
        evolution_instance=INSTANCE,
        couriers_group_jid=COURIERS_GROUP_JID,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):  # real engine and arq pool
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def _rows(session_factory: async_sessionmaker[AsyncSession]) -> list[InboundEvent]:
    async with session_factory() as session:
        return list((await session.scalars(select(InboundEvent).order_by(InboundEvent.id))).all())


async def test_new_event_is_stored_and_one_job_enqueued(
    client: httpx.AsyncClient,
    arq_redis: ArqRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    resp = await client.post(URL, json=load_fixture("private_text"))
    assert resp.status_code == 200
    (row,) = await _rows(session_factory)
    assert resp.json() == {"status": "accepted", "inbound_event_id": row.id}
    assert row.wa_message_id == "3EB0A1B2C3D4E5F60001"
    assert row.status == "received"
    assert row.payload["data"]["key"]["id"] == "3EB0A1B2C3D4E5F60001"
    assert await queued(arq_redis) == [("process_inbound_event", (row.id,))]


async def test_sequential_duplicate_deliveries_give_one_row_and_one_job(
    client: httpx.AsyncClient,
    arq_redis: ArqRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    statuses = [
        (await client.post(URL, json=load_fixture("group_reaction"))).json()["status"]
        for _ in range(3)
    ]
    assert statuses == ["accepted", "duplicate", "duplicate"]
    assert len(await _rows(session_factory)) == 1
    assert len(await queued(arq_redis)) == 1


async def test_concurrent_duplicate_deliveries_give_one_row_and_one_job(
    client: httpx.AsyncClient,
    arq_redis: ArqRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    payload = load_fixture("private_text")
    responses = await asyncio.gather(*(client.post(URL, json=payload) for _ in range(10)))
    statuses = sorted(r.json()["status"] for r in responses)
    assert all(r.status_code == 200 for r in responses)
    assert statuses == ["accepted"] + ["duplicate"] * 9
    assert len(await _rows(session_factory)) == 1
    assert len(await queued(arq_redis)) == 1


async def test_distinct_events_each_get_a_row_and_a_job(
    client: httpx.AsyncClient,
    arq_redis: ArqRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    names = ["private_text", "group_reaction", "group_reaction_removal", "connection_update_close"]
    await asyncio.gather(*(client.post(URL, json=load_fixture(n)) for n in names))
    rows = await _rows(session_factory)
    assert len(rows) == 4
    assert sorted(a for (_, (a,)) in await queued(arq_redis)) == sorted(r.id for r in rows)


async def test_filtered_events_are_not_stored(
    client: httpx.AsyncClient,
    arq_redis: ArqRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    for name in ("private_from_me", "status_broadcast", "other_group_reaction", "group_text"):
        assert (await client.post(URL, json=load_fixture(name))).json()["status"] == "ignored"
    assert await _rows(session_factory) == []
    assert await queued(arq_redis) == []


async def test_stored_payload_has_no_secrets(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    payload = load_fixture("private_text")
    payload["destination"] = f"http://web:8000{URL}"
    payload["apikey"] = "instance-key"
    await client.post(URL, json=payload)
    async with session_factory() as session:
        raw = await session.scalar(select(InboundEvent.payload))
    assert SECRET not in str(raw)
    assert "instance-key" not in str(raw)
