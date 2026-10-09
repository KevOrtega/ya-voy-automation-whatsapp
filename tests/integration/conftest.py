"""Fixtures for tests against the real compose Postgres (RUN_INTEGRATION=1).

A dedicated database (``yavoy_test``) is recreated once per session and migrated with Alembic;
every test gets a fresh engine and all tables are truncated afterwards. Concurrency tests need
committed data visible across connections, so transaction-rollback isolation is not used.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest
from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from tests.conftest import ORIGINAL_ENV
from tests.integration.dbutil import alembic, drop_database, recreate_database, url_for
from ya_voy.config import Settings
from ya_voy.db.base import Base
from ya_voy.whatsapp.client import EvolutionClient

TEST_DB = "yavoy_test"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    here = Path(__file__).parent
    skip = pytest.mark.skip(reason="set RUN_INTEGRATION=1 (needs the compose Postgres/Redis)")
    for item in items:
        if here in Path(item.fspath).parents:
            item.add_marker(pytest.mark.integration)
            if ORIGINAL_ENV.get("RUN_INTEGRATION") != "1":
                item.add_marker(skip)


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    recreate_database(TEST_DB)
    url = url_for(TEST_DB)
    alembic(url, "upgrade", "head")
    yield url
    drop_database(TEST_DB)


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
        async with engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
            await conn.execute(text("ALTER SEQUENCE order_code_seq RESTART"))
        await engine.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


# --- Redis / arq ---------------------------------------------------------------------------

TEST_REDIS_DB = 15


def redis_test_url() -> str:
    """``REDIS_URL`` pointed at a dedicated database, flushed around every test."""
    parts = urlsplit(ORIGINAL_ENV["REDIS_URL"])
    return urlunsplit(parts._replace(path=f"/{TEST_REDIS_DB}"))


@pytest.fixture
async def arq_redis() -> AsyncIterator[ArqRedis]:
    redis = await create_pool(RedisSettings.from_dsn(redis_test_url()))
    await redis.flushdb()
    try:
        yield redis
    finally:
        await redis.flushdb()
        await redis.aclose()


async def queued(redis: ArqRedis, function: str | None = None) -> list[tuple[str, tuple]]:
    """Jobs waiting in the arq queue (including deferred ones) as ``(function, args)``."""
    jobs = [(j.function, tuple(j.args)) for j in await redis.queued_jobs()]
    return [j for j in jobs if function is None or j[0] == function]


@pytest.fixture
async def worker_ctx(
    session_factory: async_sessionmaker[AsyncSession], arq_redis: ArqRedis
) -> AsyncIterator[Callable[..., dict[str, Any]]]:
    """Build the ``ctx`` dict arq passes to jobs; ``settings`` overrides go in kwargs."""
    clients: list[EvolutionClient] = []

    def build(**settings: Any) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "evolution_base_url": EVOLUTION_URL,
            "evolution_api_key": "evo-test-key",
            "evolution_instance": "yavoy",
            "couriers_group_jid": "120363000000000001@g.us",
            "admin_phones": "5491100000001,5491100000002",
            "outbound_min_interval_ms": 0,
        }
        s = Settings(_env_file=None, **(defaults | settings))
        client = EvolutionClient.from_settings(s)
        clients.append(client)
        return {
            "settings": s,
            "session_factory": session_factory,
            "redis": arq_redis,
            "evolution": client,
        }

    yield build
    for client in clients:
        await client.aclose()


EVOLUTION_URL = "http://evolution.test:8080"
