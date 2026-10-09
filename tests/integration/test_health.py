"""Integration tests against the real compose Postgres and Redis.

Run inside Docker: ``make test-docker`` (sets RUN_INTEGRATION=1). Skipped otherwise.
"""

from __future__ import annotations

import httpx

from tests.conftest import ORIGINAL_ENV as _ENV
from ya_voy.api.main import create_app
from ya_voy.config import Settings


def _real_settings() -> Settings:
    return Settings(
        _env_file=None,
        database_url=_ENV["DATABASE_URL"],
        redis_url=_ENV["REDIS_URL"],
        webhook_secret="integration-secret",
    )


async def test_health_against_real_postgres_and_redis() -> None:
    app = create_app(_real_settings())
    async with app.router.lifespan_context(app):  # opens the real engine and Redis pool
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "checks": {"database": "ok", "redis": "ok"}}
