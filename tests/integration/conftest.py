"""Fixtures for tests against the real compose Postgres (RUN_INTEGRATION=1).

A dedicated database (``yavoy_test``) is recreated once per session and migrated with Alembic;
every test gets a fresh engine and all tables are truncated afterwards. Concurrency tests need
committed data visible across connections, so transaction-rollback isolation is not used.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
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
from ya_voy.db.base import Base

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
