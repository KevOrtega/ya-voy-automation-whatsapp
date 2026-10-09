"""FastAPI application: Evolution webhook receiver and health endpoint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from arq import create_pool
from fastapi import FastAPI

from ya_voy import __version__
from ya_voy.api import health, webhooks
from ya_voy.config import Settings, get_settings
from ya_voy.db.session import create_engine
from ya_voy.logging import configure_logging
from ya_voy.queue import redis_settings_from_url


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    app.state.engine = create_engine(settings)
    # The arq pool doubles as the Redis client for the health check and, from stage 3, enqueueing.
    app.state.redis = await create_pool(redis_settings_from_url(settings.redis_url))
    try:
        yield
    finally:
        await app.state.redis.aclose()
        await app.state.engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    app = FastAPI(
        title="Ya Voy",
        version=__version__,
        lifespan=lifespan,
        # No public API besides the webhook: hide the interactive docs outside development.
        docs_url="/docs" if settings.is_development else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.is_development else None,
    )
    app.state.settings = settings
    app.include_router(health.router)
    app.include_router(webhooks.router)
    return app


app = create_app()
