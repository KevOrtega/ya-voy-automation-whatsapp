"""Webhook handler with the database and the queue faked (real ones: tests/integration)."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.exc import OperationalError

from tests.conftest import make_settings
from tests.evolution_fixtures import COURIERS_GROUP_JID, INSTANCE, load_fixture
from ya_voy.api import webhooks
from ya_voy.api.dependencies import get_redis, get_session_factory
from ya_voy.api.main import create_app

SECRET = "test-secret-123"
URL = f"/webhooks/evolution/{SECRET}"


class FakeSession:
    @asynccontextmanager
    async def begin(self) -> AsyncIterator[None]:
        yield


class FakeSessionFactory:
    def __call__(self) -> Any:
        @asynccontextmanager
        async def _cm() -> AsyncIterator[FakeSession]:
            yield FakeSession()

        return _cm()


class FakeStore:
    """Stands in for inbound_events with its unique wa_message_id."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.fail: Exception | None = None

    async def insert(self, session: Any, **values: Any) -> int | None:
        if self.fail:
            raise self.fail
        if values["wa_message_id"] in self.rows:
            return None
        self.rows[values["wa_message_id"]] = values
        return len(self.rows)


class FakeRedis:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple[Any, ...]]] = []
        self.fail = False

    async def enqueue_job(self, name: str, *args: Any, **kwargs: Any) -> None:
        if self.fail:
            raise ConnectionError("redis down")
        self.jobs.append((name, args))


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    fake = FakeStore()
    monkeypatch.setattr(webhooks, "insert_inbound_event", fake.insert)
    return fake


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis()


def _app(redis: FakeRedis, **settings: Any) -> FastAPI:
    defaults: dict[str, Any] = {
        "webhook_secret": SECRET,
        "evolution_instance": INSTANCE,
        "couriers_group_jid": COURIERS_GROUP_JID,
    }
    app = create_app(make_settings(**(defaults | settings)))
    app.dependency_overrides[get_session_factory] = FakeSessionFactory
    app.dependency_overrides[get_redis] = lambda: redis
    return app


@pytest.fixture
async def client(redis: FakeRedis) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=_app(redis))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_new_message_is_stored_and_enqueued(
    client: httpx.AsyncClient, store: FakeStore, redis: FakeRedis
) -> None:
    resp = await client.post(URL, json=load_fixture("private_text"))
    assert resp.status_code == 200
    assert resp.json() == {"status": "accepted", "inbound_event_id": 1}
    row = store.rows["3EB0A1B2C3D4E5F60001"]
    assert row["event_type"] == "messages.upsert"
    assert row["instance"] == INSTANCE
    assert row["remote_jid"] == "5491155550001@s.whatsapp.net"
    assert row["participant"] is None
    assert redis.jobs == [("process_inbound_event", (1,))]


async def test_duplicate_delivery_is_acknowledged_without_a_second_job(
    client: httpx.AsyncClient, store: FakeStore, redis: FakeRedis
) -> None:
    first = await client.post(URL, json=load_fixture("private_text"))
    second = await client.post(URL, json=load_fixture("private_text"))
    assert first.json()["status"] == "accepted"
    assert second.status_code == 200
    assert second.json() == {"status": "duplicate"}
    assert len(store.rows) == 1
    assert len(redis.jobs) == 1


async def test_group_reaction_keeps_participant(
    client: httpx.AsyncClient, store: FakeStore
) -> None:
    await client.post(URL, json=load_fixture("group_reaction"))
    row = store.rows["3EB0A1B2C3D4E5F60007"]
    assert row["remote_jid"] == COURIERS_GROUP_JID
    assert row["participant"] == "200000000000001@lid"


async def test_connection_update_is_stored_with_a_hash_key(
    client: httpx.AsyncClient, store: FakeStore, redis: FakeRedis
) -> None:
    resp = await client.post(URL, json=load_fixture("connection_update_close"))
    assert resp.json()["status"] == "accepted"
    (key,) = store.rows
    assert key.startswith("sha256:")
    assert store.rows[key]["event_type"] == "connection.update"
    assert store.rows[key]["remote_jid"] is None


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("private_from_me", "from_me"),
        ("status_broadcast", "status_broadcast"),
        ("other_group_reaction", "other_group"),
        ("group_text", "group_not_reaction"),
        ("unsupported_event", "unsupported_event"),
    ],
)
async def test_irrelevant_events_are_acknowledged_and_dropped(
    client: httpx.AsyncClient, store: FakeStore, redis: FakeRedis, name: str, reason: str
) -> None:
    resp = await client.post(URL, json=load_fixture(name))
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "reason": reason}
    assert store.rows == {}
    assert redis.jobs == []


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"\xff\xfe",
        b"[1, 2]",
        b'"text"',
        b"{}",
        json.dumps({"event": "messages.upsert", "data": {"no": "key"}}).encode(),
    ],
)
async def test_malformed_payload_is_a_400_never_a_500(
    client: httpx.AsyncClient, store: FakeStore, redis: FakeRedis, body: bytes
) -> None:
    # 400 is in Evolution's non-retryable list, so a bad payload is not retried forever.
    resp = await client.post(URL, content=body, headers={"content-type": "application/json"})
    assert resp.status_code == 400
    assert resp.json()["status"] == "rejected"
    assert store.rows == {}
    assert redis.jobs == []


async def test_database_down_returns_503_so_evolution_retries(
    client: httpx.AsyncClient, store: FakeStore, redis: FakeRedis
) -> None:
    store.fail = OperationalError("INSERT", {}, ConnectionError("db down"))
    resp = await client.post(URL, json=load_fixture("private_text"))
    assert resp.status_code == 503
    assert redis.jobs == []


async def test_enqueue_failure_still_acknowledges(
    client: httpx.AsyncClient, store: FakeStore, redis: FakeRedis
) -> None:
    # The row is stored; recover_inbound_events re-enqueues it later.
    redis.fail = True
    resp = await client.post(URL, json=load_fixture("private_text"))
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"
    assert len(store.rows) == 1


async def test_secret_envelope_fields_are_never_stored(
    client: httpx.AsyncClient, store: FakeStore
) -> None:
    payload = load_fixture("private_text")
    payload["destination"] = f"http://web:8000/webhooks/evolution/{SECRET}"
    payload["apikey"] = "instance-api-key"
    await client.post(URL, json=payload)
    stored = json.dumps(store.rows["3EB0A1B2C3D4E5F60001"]["payload"])
    assert SECRET not in stored
    assert "instance-api-key" not in stored


async def test_webhook_logs_never_contain_secret_or_message_text(
    client: httpx.AsyncClient, store: FakeStore, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    payload = load_fixture("private_text")
    payload["destination"] = f"http://web:8000/webhooks/evolution/{SECRET}"
    for name in ("private_text", "group_text"):
        await client.post(URL, json=load_fixture(name) | {"destination": payload["destination"]})
    await client.post(URL, content=b"not json")
    # Only the app's records: the test client's own httpx log line shows the raw URL (in the
    # running app that goes through the redaction filter, see test_log_redaction.py).
    records = [r for r in caplog.records if r.name.startswith("ya_voy")]
    assert records
    text = " ".join(f"{r.getMessage()} {r.__dict__}" for r in records)
    assert SECRET not in text
    assert "Dirección de retiro" not in text
    assert "almendros" not in text


# --- capture mode --------------------------------------------------------------------------


async def _post_with(redis: FakeRedis, body: Any, **settings: Any) -> httpx.Response:
    transport = httpx.ASGITransport(app=_app(redis, **settings))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.post(URL, json=body)


async def test_capture_mode_writes_every_payload_redacted(
    tmp_path: Path, store: FakeStore, redis: FakeRedis
) -> None:
    payload = load_fixture("group_text")  # dropped by the filters but still captured
    payload["destination"] = f"http://web:8000/webhooks/evolution/{SECRET}"
    resp = await _post_with(
        redis, payload, capture_inbound_payloads=True, capture_dir=str(tmp_path)
    )
    assert resp.json()["status"] == "ignored"
    (path,) = tmp_path.iterdir()
    assert path.name.endswith("_messages.upsert_" + path.name.split("_")[-1])
    content = path.read_text(encoding="utf-8")
    assert SECRET not in content
    assert json.loads(content)["data"] == payload["data"]


async def test_capture_mode_off_by_default(
    tmp_path: Path, store: FakeStore, redis: FakeRedis
) -> None:
    await _post_with(redis, load_fixture("private_text"), capture_dir=str(tmp_path))
    assert list(tmp_path.iterdir()) == []


async def test_capture_write_failure_does_not_break_the_webhook(
    tmp_path: Path, store: FakeStore, redis: FakeRedis
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    resp = await _post_with(
        redis,
        load_fixture("private_text"),
        capture_inbound_payloads=True,
        capture_dir=str(blocker / "sub"),
    )
    assert resp.json()["status"] == "accepted"
