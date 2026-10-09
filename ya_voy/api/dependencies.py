"""FastAPI dependencies. Overridable in tests via ``app.dependency_overrides``."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from arq.connections import ArqRedis
from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ya_voy.config import Settings

HealthCheck = Callable[[], Awaitable[None]]


def settings_dependency(request: Request) -> Settings:
    return request.app.state.settings


def get_engine(request: Request) -> AsyncEngine:
    return request.app.state.engine


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    return request.app.state.session_factory


def get_redis(request: Request) -> ArqRedis:
    return request.app.state.redis


def get_health_checks(request: Request) -> dict[str, HealthCheck]:
    """Named connectivity checks run by ``GET /health``; each raises on failure."""
    engine = get_engine(request)
    redis = get_redis(request)

    async def check_database() -> None:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    async def check_redis() -> None:
        await redis.ping()

    return {"database": check_database, "redis": check_redis}
