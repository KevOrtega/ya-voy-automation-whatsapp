from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from tests.conftest import make_settings
from ya_voy.api.dependencies import get_health_checks
from ya_voy.api.main import create_app

SECRET = "test-secret-123"


async def _ok() -> None:
    return None


async def _fail() -> None:
    raise ConnectionError("down")


@pytest.fixture
def app() -> FastAPI:
    # The lifespan (real DB/Redis connections) is not run by ASGITransport.
    return create_app(make_settings(webhook_secret=SECRET))


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_health_ok(app: FastAPI, client: httpx.AsyncClient) -> None:
    app.dependency_overrides[get_health_checks] = lambda: {"database": _ok, "redis": _ok}
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "checks": {"database": "ok", "redis": "ok"}}


@pytest.mark.parametrize("failing", ["database", "redis"])
async def test_health_reports_failing_dependency(
    app: FastAPI, client: httpx.AsyncClient, failing: str
) -> None:
    checks = {"database": _ok, "redis": _ok, failing: _fail}
    app.dependency_overrides[get_health_checks] = lambda: checks
    resp = await client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "error"
    assert body["checks"][failing] == "error"


async def test_webhook_accepts_valid_secret(client: httpx.AsyncClient) -> None:
    payload = {"event": "messages.upsert", "instance": "yavoy", "data": {}}
    resp = await client.post(f"/webhooks/evolution/{SECRET}", json=payload)
    assert resp.status_code == 200
    assert resp.json() == {"status": "accepted"}


@pytest.mark.parametrize("secret", ["wrong", SECRET + "x", SECRET[:-1], "%20"])
async def test_webhook_rejects_wrong_secret(client: httpx.AsyncClient, secret: str) -> None:
    resp = await client.post(f"/webhooks/evolution/{secret}", json={})
    assert resp.status_code == 404


async def test_webhook_rejects_everything_when_secret_unset() -> None:
    app = create_app(make_settings(webhook_secret=""))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        assert (await c.post("/webhooks/evolution/anything", json={})).status_code == 404


async def test_webhook_only_accepts_post(client: httpx.AsyncClient) -> None:
    resp = await client.get(f"/webhooks/evolution/{SECRET}")
    assert resp.status_code == 405
