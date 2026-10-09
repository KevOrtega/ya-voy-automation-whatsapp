"""send_outbound against the real Postgres/Redis with Evolution mocked by respx (docs/04 §9)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
import respx
from arq.connections import ArqRedis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tests.integration import factories as f
from tests.integration.conftest import EVOLUTION_URL, queued
from ya_voy.db.models import Message, OutboundMessage
from ya_voy.messaging import catalog
from ya_voy.worker import outbound
from ya_voy.worker.outbound import BACKOFF_KEY, INTERVAL_LOCK_KEY, send_outbound

Ctx = Callable[..., dict[str, Any]]
STORE_JID = "5491155550001@s.whatsapp.net"
GROUP_JID = "120363000000000001@g.us"


@pytest.fixture
def evo() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=EVOLUTION_URL, assert_all_called=False) as router:
        counter = iter(range(1, 10_000))
        router.post("/message/sendText/yavoy", name="sendText").mock(
            side_effect=lambda request: httpx.Response(
                201, json={"key": {"id": f"BAE5SENT{next(counter)}", "fromMe": True}}
            )
        )
        router.post("/message/sendMedia/yavoy", name="sendMedia").respond(
            201, json={"key": {"id": "BAE5DOC"}}
        )
        router.delete("/chat/deleteMessageForEveryone/yavoy", name="delete").respond(
            201, json={"key": {"id": "BAE5DEL"}}
        )
        router.post("/chat/updateMessage/yavoy", name="update").respond(
            201, json={"key": {"id": "BAE5EDIT"}}
        )
        yield router


def bodies(route: respx.Route) -> list[dict[str, Any]]:
    return [json.loads(call.request.content) for call in route.calls]


async def get_row(
    session_factory: async_sessionmaker[AsyncSession], row_id: int
) -> OutboundMessage:
    async with session_factory() as session:
        row = await session.get(OutboundMessage, row_id)
        assert row is not None
        return row


async def test_text_is_sent_and_recorded(
    engine: AsyncEngine,
    worker_ctx: Ctx,
    evo: respx.MockRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    row_id = await f.insert_ok(
        engine, f.outbound(recipient_jid=GROUP_JID, body="✅ asignado", quoted_wa_message_id="P1")
    )
    assert await send_outbound(worker_ctx(), row_id) == "sent"
    assert bodies(evo["sendText"]) == [
        {"number": GROUP_JID, "text": "✅ asignado", "quoted": {"key": {"id": "P1"}}}
    ]
    row = await get_row(session_factory, row_id)
    assert (row.status, row.attempts, row.wa_message_id) == ("sent", 1, "BAE5SENT1")
    assert row.sent_at is not None
    async with session_factory() as session:
        (msg,) = (await session.scalars(select(Message))).all()
    assert (msg.direction, msg.chat_jid, msg.kind, msg.body) == (
        "out",
        GROUP_JID,
        "text",
        "✅ asignado",
    )
    assert (msg.wa_message_id, msg.outbound_message_id) == ("BAE5SENT1", row_id)


async def test_redelivered_job_does_not_send_twice(
    engine: AsyncEngine, worker_ctx: Ctx, evo: respx.MockRouter
) -> None:
    row_id = await f.insert_ok(engine, f.outbound(body="hola"))
    ctx = worker_ctx()
    assert await send_outbound(ctx, row_id) == "sent"
    assert await send_outbound(ctx, row_id) == "skipped:sent"
    assert evo["sendText"].call_count == 1


async def test_concurrent_jobs_for_the_same_row_send_once(
    engine: AsyncEngine, worker_ctx: Ctx, evo: respx.MockRouter
) -> None:
    row_id = await f.insert_ok(engine, f.outbound(body="hola"))
    original = evo["sendText"].side_effect

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.2)  # keep the row locked while the duplicates arrive
        return original(request)

    evo["sendText"].side_effect = slow
    ctx = worker_ctx()
    outcomes = await asyncio.gather(*(send_outbound(ctx, row_id) for _ in range(5)))
    assert outcomes.count("sent") == 1
    assert all(o in ("sent", "skipped:locked_or_missing", "skipped:sent") for o in outcomes)
    assert evo["sendText"].call_count == 1


async def test_fifo_per_recipient(
    engine: AsyncEngine, worker_ctx: Ctx, evo: respx.MockRouter, arq_redis: ArqRedis
) -> None:
    first = await f.insert_ok(engine, f.outbound(recipient_jid=STORE_JID, body="1"))
    second = await f.insert_ok(engine, f.outbound(recipient_jid=STORE_JID, body="2"))
    other = await f.insert_ok(engine, f.outbound(recipient_jid=GROUP_JID, body="g"))
    ctx = worker_ctx()

    assert await send_outbound(ctx, second) == "waiting_fifo"
    assert evo["sendText"].call_count == 0
    assert await queued(arq_redis, "send_outbound") == [("send_outbound", (second,))]
    assert await send_outbound(ctx, other) == "sent"  # other recipients are not blocked

    assert await send_outbound(ctx, first) == "sent"
    assert await send_outbound(ctx, second) == "sent"
    assert [b["text"] for b in bodies(evo["sendText"])] == ["g", "1", "2"]


async def test_fifo_is_not_blocked_by_a_failed_earlier_row(
    engine: AsyncEngine, worker_ctx: Ctx, evo: respx.MockRouter
) -> None:
    await f.insert_ok(engine, f.outbound(recipient_jid=STORE_JID, status="failed"))
    later = await f.insert_ok(engine, f.outbound(recipient_jid=STORE_JID, body="2"))
    assert await send_outbound(worker_ctx(), later) == "sent"


async def test_min_interval_between_sends(
    engine: AsyncEngine, worker_ctx: Ctx, evo: respx.MockRouter, arq_redis: ArqRedis
) -> None:
    a = await f.insert_ok(engine, f.outbound(recipient_jid=STORE_JID))
    b = await f.insert_ok(engine, f.outbound(recipient_jid=GROUP_JID))
    ctx = worker_ctx(outbound_min_interval_ms=5_000)
    assert await send_outbound(ctx, a) == "sent"
    assert await send_outbound(ctx, b) == "throttled"
    assert evo["sendText"].call_count == 1
    assert 0 < await arq_redis.pttl(INTERVAL_LOCK_KEY) <= 5_000
    assert await queued(arq_redis, "send_outbound") == [("send_outbound", (b,))]
    await arq_redis.delete(INTERVAL_LOCK_KEY)  # the interval elapsed
    assert await send_outbound(ctx, b) == "sent"


async def test_error_retries_with_backoff_then_fails_and_alerts(
    engine: AsyncEngine,
    worker_ctx: Ctx,
    evo: respx.MockRouter,
    arq_redis: ArqRedis,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    evo["sendText"].side_effect = None
    evo["sendText"].respond(500, json={"error": "boom"})
    store_id = await f.insert_ok(engine, f.store(wa_jid=STORE_JID, display_name="Pizzería Test"))
    assert store_id
    row_id = await f.insert_ok(
        engine, f.outbound(recipient_jid=STORE_JID, template_key="store_order_published")
    )
    ctx = worker_ctx(outbound_max_attempts=3)

    assert await send_outbound(ctx, row_id) == "retry"
    row = await get_row(session_factory, row_id)
    assert (row.status, row.attempts) == ("pending", 1)
    assert "HTTP 500" in (row.last_error or "")
    assert 0 < await arq_redis.pttl(BACKOFF_KEY.format(id=row_id)) <= 2_000
    assert await queued(arq_redis, "send_outbound") == [("send_outbound", (row_id,))]

    # A duplicate or swept job during the backoff does not call Evolution again.
    assert await send_outbound(ctx, row_id) == "backoff"
    assert evo["sendText"].call_count == 1

    await arq_redis.delete(BACKOFF_KEY.format(id=row_id))
    assert await send_outbound(ctx, row_id) == "retry"
    await arq_redis.delete(BACKOFF_KEY.format(id=row_id))
    assert await send_outbound(ctx, row_id) == "failed"
    assert evo["sendText"].call_count == 3

    row = await get_row(session_factory, row_id)
    assert (row.status, row.attempts) == ("failed", 3)
    async with session_factory() as session:
        alerts = (
            await session.scalars(
                select(OutboundMessage).where(
                    OutboundMessage.template_key == "admin_outbound_failed"
                )
            )
        ).all()
    assert len(alerts) == 2  # one per ADMIN_PHONES entry
    assert alerts[0].body == catalog.render(
        catalog.ADMIN_OUTBOUND_FAILED,
        template_key="store_order_published",
        recipient_name="Pizzería Test",
        attempts=3,
    )
    alert_jobs = {a for (_, (a,)) in await queued(arq_redis, "send_outbound")}
    assert {a.id for a in alerts} <= alert_jobs
    assert await send_outbound(ctx, row_id) == "skipped:failed"


async def test_failed_admin_alert_does_not_alert_again(
    engine: AsyncEngine,
    worker_ctx: Ctx,
    evo: respx.MockRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    evo["sendText"].side_effect = None
    evo["sendText"].respond(500)
    row_id = await f.insert_ok(engine, f.outbound(template_key="admin_outbound_failed"))
    assert await send_outbound(worker_ctx(outbound_max_attempts=1), row_id) == "failed"
    async with session_factory() as session:
        count = len((await session.scalars(select(OutboundMessage))).all())
    assert count == 1


async def test_group_recipient_name_in_failure_alert(
    engine: AsyncEngine,
    worker_ctx: Ctx,
    evo: respx.MockRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    evo["delete"].respond(400, json={"message": "not found"})
    row_id = await f.insert_ok(
        engine,
        f.outbound(
            recipient_jid=GROUP_JID,
            message_type="delete",
            target_wa_message_id="BAE5POST",
            template_key="group_post_delete",
        ),
    )
    assert await send_outbound(worker_ctx(outbound_max_attempts=1), row_id) == "failed"
    async with session_factory() as session:
        alert = await session.scalar(
            select(OutboundMessage).where(OutboundMessage.template_key == "admin_outbound_failed")
        )
    assert alert is not None
    assert catalog.COURIERS_GROUP_NAME in alert.body


async def test_delete_and_edit_call_their_endpoints_without_history_rows(
    engine: AsyncEngine,
    worker_ctx: Ctx,
    evo: respx.MockRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    delete_id = await f.insert_ok(
        engine,
        f.outbound(recipient_jid=GROUP_JID, message_type="delete", target_wa_message_id="P1"),
    )
    edit_id = await f.insert_ok(
        engine,
        f.outbound(
            recipient_jid=GROUP_JID,
            message_type="edit",
            target_wa_message_id="P1",
            body="Pedido cancelado",
        ),
    )
    ctx = worker_ctx()
    assert await send_outbound(ctx, delete_id) == "sent"
    assert await send_outbound(ctx, edit_id) == "sent"
    assert bodies(evo["delete"]) == [{"id": "P1", "remoteJid": GROUP_JID, "fromMe": True}]
    assert bodies(evo["update"])[0]["text"] == "Pedido cancelado"
    async with session_factory() as session:
        assert (await session.scalars(select(Message))).all() == []


async def test_document_is_sent_from_the_report_revision(
    engine: AsyncEngine,
    worker_ctx: Ctx,
    evo: respx.MockRouter,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    report_id = await f.insert_ok(engine, f.daily_report())
    revision_id = await f.insert_ok(
        engine,
        f.revision(
            report_id, status="generated", file_name="reporte.xlsx", file_content=b"PK\x03\x04"
        ),
    )
    row_id = await f.insert_ok(
        engine,
        f.outbound(message_type="document", report_revision_id=revision_id, body="Reporte"),
    )
    assert await send_outbound(worker_ctx(), row_id) == "sent"
    (body,) = bodies(evo["sendMedia"])
    assert body["media"] == "UEsDBA=="
    assert (body["fileName"], body["caption"], body["mediatype"]) == (
        "reporte.xlsx",
        "Reporte",
        "document",
    )
    async with session_factory() as session:
        (msg,) = (await session.scalars(select(Message))).all()
    assert msg.kind == "document"


def test_backoff_grows() -> None:
    assert outbound.backoff_delay(1) < outbound.backoff_delay(2) < outbound.backoff_delay(3)
    assert outbound.backoff_delay(50) == timedelta(seconds=outbound.BACKOFF_MAX_SECONDS)
