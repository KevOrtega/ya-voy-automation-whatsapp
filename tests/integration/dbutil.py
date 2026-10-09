"""Helpers to create, migrate and drop throwaway databases on the compose Postgres."""

from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Callable
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import ORIGINAL_ENV
from ya_voy.config import to_async_database_url

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_URL = "postgresql+asyncpg://yavoy:yavoy@localhost:5432/yavoy"


def base_url() -> str:
    return to_async_database_url(ORIGINAL_ENV.get("DATABASE_URL", DEFAULT_URL))


def url_for(database: str) -> str:
    return make_url(base_url()).set(database=database).render_as_string(hide_password=False)


def in_thread(fn: Callable[..., Any], *args: Any) -> Any:
    """Run ``fn`` outside any running event loop (Alembic's env.py calls asyncio.run)."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(fn, *args).result()


def recreate_database(name: str) -> None:
    async def run() -> None:
        engine = create_async_engine(base_url(), poolclass=NullPool, isolation_level="AUTOCOMMIT")
        async with engine.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
        await engine.dispose()

    in_thread(asyncio.run, run())


def drop_database(name: str) -> None:
    async def run() -> None:
        engine = create_async_engine(base_url(), poolclass=NullPool, isolation_level="AUTOCOMMIT")
        async with engine.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await engine.dispose()

    in_thread(asyncio.run, run())


def alembic(database_url: str, cmd: str, *args: Any) -> None:
    """Run an Alembic command (``upgrade``, ``downgrade``, ``check``...) on ``database_url``."""
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "ya_voy/db/migrations"))
    cfg.attributes["database_url"] = database_url
    cfg.attributes["configure_logging"] = False
    in_thread(getattr(command, cmd), cfg, *args)
