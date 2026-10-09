"""process_inbound_event: idempotent on re-delivery and concurrency; rate limit (CU-11, CU-18)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import pytest
from arq.connections import ArqRedis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.evolution_fixtures import load_fixture
from tests.integration.conftest import queued
from ya_voy.db.models import InboundEvent, OutboundMessage, Store
from ya_voy.db.queries import insert_inbound_event
from ya_voy.messaging import catalog
from ya_voy.whatsapp.dedup import compute_inbound_dedup_key
from ya_voy.whatsapp.payloads import InboundMessage, parse_webhook
from ya_voy.worker import inbound
from ya_voy.worker.inbound import process_inbound_event

Ctx = Callable[..., dict[str, Any]]


async def store_event(
    session_factory: async_sessionmaker[AsyncSession], payload: dict[str, Any]
) -> int:
    event = parse_webhook(payload)
    message = event if isinstance(event, InboundMessage) else None
    async with session_factory() as session, session.begin():
        event_id = await insert_inbound_event(
            session,
            wa_message_id=compute_inbound_dedup_key(event, payload),
            instance="yavoy",
            event_type=event.event_type,
            remote_jid=message.remote_jid if message else None,
            participant=message.participant if message else None,
            payload=payload,
        )
    assert event_id is not None
    return event_id


async def get_event(
    session_factory: async_sessionmaker[AsyncSession], event_id: int
) -> InboundEvent:
    async with session_factory() as session:
        row = await session.get(InboundEvent, event_id)
        assert row is not None
        return row


@pytest.fixture
def route_calls(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Records every routing decision (the stand-in for stage 4+ side effects)."""
    calls: list[Any] = []
    original = inbound.route_event

    def spy(event: Any, **kw: Any) -> Any:
        calls.append(event)
        return original(event, **kw)

    monkeypatch.setattr(inbound, "route_event", spy)
    return calls


@pytest.mark.parametrize(
    ("name", "outcome", "status"),
    [
        ("private_text", "private_text", "processed"),
        ("private_image", "private_non_text", "processed"),
        ("group_reaction", "group_reaction", "processed"),
        ("group_reaction_removal", "group_reaction_removal", "processed"),
        ("connection_update_close", "connection_update", "processed"),
        ("group_text", "unhandled", "ignored"),
    ],
)
async def test_event_is_processed_and_marked(
    worker_ctx: Ctx,
    session_factory: async_sessionmaker[AsyncSession],
    name: str,
    outcome: str,
    status: str,
) -> None:
    event_id = await store_event(session_factory, load_fixture(name))
    assert await process_inbound_event(worker_ctx(), event_id) == outcome
    row = await get_event(session_factory, event_id)
    assert row.status == status
    assert row.attempts == 1
    assert row.processed_at is not None
    assert row.error is None


async def test_redelivered_job_is_a_no_op(
    worker_ctx: Ctx, session_factory: async_sessionmaker[AsyncSession], route_calls: list[Any]
) -> None:
    event_id = await store_event(session_factory, load_fixture("private_text"))
    ctx = worker_ctx()
    assert await process_inbound_event(ctx, event_id) == "private_text"
    first = await get_event(session_factory, event_id)
    assert await process_inbound_event(ctx, event_id) == "skipped:processed"
    again = await get_event(session_factory, event_id)
    assert len(route_calls) == 1
    assert (again.attempts, again.processed_at) == (first.attempts, first.processed_at)


async def test_concurrent_jobs_for_the_same_event_process_it_once(
    worker_ctx: Ctx,
    session_factory: async_sessionmaker[AsyncSession],
    route_calls: list[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_id = await store_event(session_factory, load_fixture("group_reaction"))
    original = inbound._process_locked

    async def slow(*args: Any) -> Any:
        await asyncio.sleep(0.2)  # hold the row lock while the other jobs arrive
        return await original(*args)

    monkeypatch.setattr(inbound, "_process_locked", slow)
    ctx = worker_ctx()
    outcomes = await asyncio.gather(*(process_inbound_event(ctx, event_id) for _ in range(5)))
    assert sorted(outcomes) == ["group_reaction"] + ["skipped:processed"] * 4
    assert len(route_calls) == 1
    assert (await get_event(session_factory, event_id)).attempts == 1


async def test_missing_event_is_skipped(worker_ctx: Ctx) -> None:
    assert await process_inbound_event(worker_ctx(), 999_999) == "skipped:missing"


def _failure_records(caplog: pytest.LogCaptureFixture, event_id: int) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR
        and r.name == "ya_voy.worker.inbound"
        and getattr(r, "inbound_event_id", None) == event_id
    ]


async def test_processing_error_marks_failed_and_is_not_retried_by_redelivery(
    worker_ctx: Ctx,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def boom(*args: Any, **kw: Any) -> Any:
        raise RuntimeError("handler exploded")

    monkeypatch.setattr(inbound, "route_event", boom)
    event_id = await store_event(session_factory, load_fixture("private_text"))
    assert await process_inbound_event(worker_ctx(), event_id) == "failed"
    row = await get_event(session_factory, event_id)
    assert row.status == "failed"
    assert row.error == "RuntimeError: handler exploded"
    assert row.attempts == 1
    (record,) = _failure_records(caplog, event_id)  # never a silent failure
    assert record.error == "RuntimeError: handler exploded"
    assert await process_inbound_event(worker_ctx(), event_id) == "skipped:failed"


async def test_unparseable_stored_payload_marks_failed(
    worker_ctx: Ctx,
    session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    event_id = await store_event(session_factory, load_fixture("private_text"))
    async with session_factory() as session, session.begin():
        await session.execute(
            update(InboundEvent).where(InboundEvent.id == event_id).values(payload={"x": 1})
        )
    assert await process_inbound_event(worker_ctx(), event_id) == "failed"
    error = (await get_event(session_factory, event_id)).error
    assert error is not None and error.startswith("PayloadError")
    (record,) = _failure_records(caplog, event_id)
    assert record.error == error


# --- per-sender rate limit (RF-05, CU-18) --------------------------------------------------


def _private(n: int, jid: str = "5491155550001@s.whatsapp.net") -> dict[str, Any]:
    return load_fixture("private_text", id=f"RATE{n:04d}{jid[:6]}", remoteJid=jid)


async def _admin_alerts(session_factory: async_sessionmaker[AsyncSession]) -> list[Any]:
    async with session_factory() as session:
        return list(
            (
                await session.scalars(
                    select(OutboundMessage)
                    .where(OutboundMessage.template_key == "admin_rate_limited_sender")
                    .order_by(OutboundMessage.id)
                )
            ).all()
        )


async def test_rate_limit_ignores_excess_and_alerts_admins_once(
    worker_ctx: Ctx, session_factory: async_sessionmaker[AsyncSession], arq_redis: ArqRedis
) -> None:
    async with session_factory() as session, session.begin():
        session.add(Store(wa_jid="5491155550001@s.whatsapp.net", display_name="Pizzería Test"))
    ctx = worker_ctx(sender_rate_limit_per_minute=2)
    ids = [await store_event(session_factory, _private(i)) for i in range(4)]
    outcomes = [await process_inbound_event(ctx, i) for i in ids]
    assert outcomes == ["private_text", "private_text", "rate_limited", "rate_limited"]

    rows = [await get_event(session_factory, i) for i in ids]
    assert [r.status for r in rows] == ["processed", "processed", "ignored", "ignored"]
    assert rows[2].error == "rate_limited"

    alerts = await _admin_alerts(session_factory)
    assert {a.recipient_jid for a in alerts} == {
        "5491100000001@s.whatsapp.net",
        "5491100000002@s.whatsapp.net",
    }
    assert alerts[0].body == catalog.render(
        catalog.ADMIN_RATE_LIMITED_SENDER, sender_name="Pizzería Test"
    )
    assert "5491155550001" not in alerts[0].body  # RN-17: no phone numbers
    jobs = await queued(arq_redis, "send_outbound")
    assert sorted(a for (_, (a,)) in jobs) == sorted(a.id for a in alerts)


async def test_rate_limit_is_per_sender(
    worker_ctx: Ctx, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ctx = worker_ctx(sender_rate_limit_per_minute=1)
    a = await store_event(session_factory, _private(1, "5491155550001@s.whatsapp.net"))
    b = await store_event(session_factory, _private(2, "5491155550002@s.whatsapp.net"))
    assert await process_inbound_event(ctx, a) == "private_text"
    assert await process_inbound_event(ctx, b) == "private_text"


async def test_group_reactions_are_not_rate_limited(
    worker_ctx: Ctx, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ctx = worker_ctx(sender_rate_limit_per_minute=1)
    for i in range(3):
        event_id = await store_event(session_factory, load_fixture("group_reaction", id=f"R{i}"))
        assert await process_inbound_event(ctx, event_id) == "group_reaction"


async def test_rate_limit_alert_falls_back_to_push_name(
    worker_ctx: Ctx, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ctx = worker_ctx(sender_rate_limit_per_minute=1)
    for i in range(2):
        await process_inbound_event(ctx, await store_event(session_factory, _private(i)))
    alerts = await _admin_alerts(session_factory)
    assert alerts
    assert alerts[0].body == catalog.render(
        catalog.ADMIN_RATE_LIMITED_SENDER, sender_name="Pizzeria Test"
    )
