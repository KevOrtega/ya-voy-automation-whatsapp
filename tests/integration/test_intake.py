"""Order intake on identified stores: drafts, validation, summary, expiry (CU-01, CU-02, CU-12).

Kevin's canonical order (docs/01 §6) goes through the stub LLM; no network calls.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.integration import factories as f
from tests.integration.conftest import queued
from tests.llm_stubs import CANONICAL_ORDER_TEXT, StubExtractor
from ya_voy.db.models import Message, Order, OrderEvent, OutboundMessage, Store
from ya_voy.domain.commands import StoreCommand
from ya_voy.llm.schemas import OrderExtraction
from ya_voy.locks import store_lock_key
from ya_voy.messaging import catalog
from ya_voy.queue import EVALUATE_DRAFT, SEND_OUTBOUND
from ya_voy.worker import drafts, intake
from ya_voy.worker.intake import StoreText

Ctx = Callable[..., dict[str, Any]]
SF = async_sessionmaker[AsyncSession]

CANONICAL_SUMMARY = (
    "📝 Revisá tu pedido:\n\n"
    "🏪 *Local:* Casa Nogal\n"
    "📍 *Retiro:* casa nogal almendros\n"
    "📍 *Entrega:* el ceibo 2871\n"
    "🥪 *Producto:* almacen\n"
    "💳 *Abona:* transferencia\n\n"
    "¿Confirmás el pedido? Respondé *SÍ* para enviarlo o corregí lo que haga falta."
)

WITHOUT_PAYMENT = "retiro casa nogal almendros, entrega el ceibo 2871, almacen"
PAYMENT_ONLY = "abona en efectivo"
CORRECTION = "perdón, la entrega es el ceibo 2900"
ANSWERS = {
    WITHOUT_PAYMENT: OrderExtraction(
        pickup_address="casa nogal almendros", delivery_address="el ceibo 2871", product="almacen"
    ),
    PAYMENT_ONLY: OrderExtraction(payment_method="cash", payment_method_raw="efectivo"),
    CORRECTION: OrderExtraction(delivery_address="el ceibo 2900"),
}


@pytest.fixture
def ctx(worker_ctx: Ctx) -> dict[str, Any]:
    c = worker_ctx(draft_debounce_seconds=0)
    c["order_extractor"] = StubExtractor(ANSWERS)
    return c


async def new_store(session_factory: SF, name: str = "Casa Nogal") -> Store:
    async with session_factory() as session, session.begin():
        store_id = await f.insert_row(await session.connection(), f.store(display_name=name))
    async with session_factory() as session:
        store = await session.get(Store, store_id)
        assert store is not None
        return store


async def send(ctx: dict[str, Any], store: Store, text: str, **kw: Any) -> intake.IntakeResult:
    return await drafts.handle_store_text(ctx, StoreText(store_id=store.id, text=text, **kw))


async def evaluate(ctx: dict[str, Any], order_id: int) -> intake.EvaluationResult:
    async with ctx["session_factory"]() as session, session.begin():
        return await intake.evaluate_draft(session, ctx["settings"], order_id)


async def get_order(session_factory: SF, order_id: int) -> Order:
    async with session_factory() as session:
        order = await session.get(Order, order_id)
        assert order is not None
        return order


async def outbox(session_factory: SF, order_id: int) -> list[OutboundMessage]:
    async with session_factory() as session:
        rows = await session.scalars(
            select(OutboundMessage)
            .where(OutboundMessage.order_id == order_id)
            .order_by(OutboundMessage.id)
        )
        return list(rows)


async def events(session_factory: SF, order_id: int) -> list[str]:
    async with session_factory() as session:
        rows = await session.scalars(
            select(OrderEvent.event_type)
            .where(OrderEvent.order_id == order_id)
            .order_by(OrderEvent.id)
        )
        return list(rows)


async def store_orders(session_factory: SF, store: Store) -> list[Order]:
    async with session_factory() as session:
        return list(await session.scalars(select(Order).where(Order.store_id == store.id)))


# --- the canonical order ---------------------------------------------------------------------


async def test_canonical_order_creates_a_complete_draft(ctx: dict[str, Any]) -> None:
    store = await new_store(ctx["session_factory"])
    result = await send(ctx, store, CANONICAL_ORDER_TEXT, wa_message_id="WAM-canonical")

    assert (result.outcome, result.evaluate, result.outbound_ids) == ("draft_created", True, [])
    order = await get_order(ctx["session_factory"], result.order_id)
    assert order.status == "draft"
    assert (order.pickup_address, order.delivery_address, order.product) == (
        "casa nogal almendros",
        "el ceibo 2871",
        "almacen",
    )
    assert (order.payment_method, order.payment_method_raw, order.notes) == (
        "transfer",
        "transferencia",
        None,
    )
    assert order.extraction["payment_method"] == "transfer"
    assert order.code is None  # codes are assigned on confirmation (T06)
    ttl = ctx["settings"].draft_ttl_minutes
    assert order.draft_expires_at == order.last_store_message_at + dt.timedelta(minutes=ttl)
    assert await events(ctx["session_factory"], order.id) == ["created"]
    async with ctx["session_factory"]() as session:
        message = await session.scalar(select(Message).where(Message.order_id == order.id))
    assert message is not None
    assert (message.body, message.wa_message_id, message.direction) == (
        CANONICAL_ORDER_TEXT,
        "WAM-canonical",
        "in",
    )


async def test_canonical_order_gets_the_confirmation_summary(ctx: dict[str, Any]) -> None:
    store = await new_store(ctx["session_factory"])
    result = await send(ctx, store, CANONICAL_ORDER_TEXT)

    evaluation = await evaluate(ctx, result.order_id)

    assert evaluation.outcome == "confirmation_requested"
    order = await get_order(ctx["session_factory"], result.order_id)
    assert order.status == "awaiting_confirmation"
    assert order.confirmation_requested_at is not None
    [summary] = await outbox(ctx["session_factory"], order.id)
    assert summary.id == evaluation.outbound_ids[0]
    assert summary.template_key == catalog.STORE_ORDER_CONFIRM_REQUEST
    assert summary.recipient_jid == store.wa_jid
    assert summary.body == CANONICAL_SUMMARY
    assert await events(ctx["session_factory"], order.id) == ["created", "confirmation_requested"]


# --- missing fields, corrections ------------------------------------------------------------


async def test_missing_payment_is_requested_then_completed(ctx: dict[str, Any]) -> None:
    store = await new_store(ctx["session_factory"])
    first = await send(ctx, store, WITHOUT_PAYMENT)
    evaluation = await evaluate(ctx, first.order_id)

    assert evaluation.outcome == "missing_fields"
    [ask] = await outbox(ctx["session_factory"], first.order_id)
    assert ask.template_key == catalog.STORE_ORDER_MISSING_FIELDS
    assert ask.body == (
        "Recibí tu pedido, pero me falta:\n"
        "Cómo abona (efectivo, transferencia, ya pagado u otro)\n\n"
        "Mandame esos datos en uno o varios mensajes. Para descartarlo, escribí *cancelar*."
    )
    order = await get_order(ctx["session_factory"], first.order_id)
    assert order.validation_errors == [{"field": "payment_method", "code": "missing"}]
    assert order.missing_prompts_count == 1

    second = await send(ctx, store, PAYMENT_ONLY)
    assert (second.outcome, second.order_id) == ("draft_updated", first.order_id)
    # The extractor received the current draft as context.
    assert ctx["order_extractor"].calls[-1][1].product == "almacen"
    assert (await evaluate(ctx, first.order_id)).outcome == "confirmation_requested"
    order = await get_order(ctx["session_factory"], first.order_id)
    assert (order.status, order.payment_method, order.validation_errors) == (
        "awaiting_confirmation",
        "cash",
        [],
    )
    summary = (await outbox(ctx["session_factory"], order.id))[-1]
    assert "💳 *Abona:* efectivo" in summary.body
    assert len(await store_orders(ctx["session_factory"], store)) == 1


async def test_format_example_after_max_missing_prompts(ctx: dict[str, Any]) -> None:
    store = await new_store(ctx["session_factory"])
    first = await send(ctx, store, WITHOUT_PAYMENT)
    await evaluate(ctx, first.order_id)
    await send(ctx, store, "hola?")  # no new data, but a new revision
    evaluation = await evaluate(ctx, first.order_id)

    assert evaluation.outcome == "missing_fields"
    keys = [m.template_key for m in await outbox(ctx["session_factory"], first.order_id)]
    assert keys == [
        catalog.STORE_ORDER_MISSING_FIELDS,
        catalog.STORE_ORDER_MISSING_FIELDS,
        catalog.STORE_ORDER_FORMAT_EXAMPLE,
    ]
    example = (await outbox(ctx["session_factory"], first.order_id))[-1]
    assert example.body == catalog.render(catalog.STORE_ORDER_FORMAT_EXAMPLE)


async def test_correction_while_awaiting_confirmation_goes_back_to_draft(
    ctx: dict[str, Any],
) -> None:
    store = await new_store(ctx["session_factory"])
    first = await send(ctx, store, CANONICAL_ORDER_TEXT)
    await evaluate(ctx, first.order_id)

    correction = await send(ctx, store, CORRECTION)

    assert (correction.outcome, correction.order_id) == ("correction", first.order_id)
    order = await get_order(ctx["session_factory"], first.order_id)
    assert (order.status, order.delivery_address, order.product) == (
        "draft",
        "el ceibo 2900",
        "almacen",
    )
    assert (await evaluate(ctx, first.order_id)).outcome == "confirmation_requested"
    summaries = await outbox(ctx["session_factory"], order.id)
    assert len(summaries) == 2
    assert "📍 *Entrega:* el ceibo 2900" in summaries[-1].body
    assert await events(ctx["session_factory"], order.id) == [
        "created",
        "confirmation_requested",
        "draft_updated",
        "confirmation_requested",
    ]


async def test_small_talk_without_a_draft_creates_nothing(ctx: dict[str, Any]) -> None:
    store = await new_store(ctx["session_factory"])
    result = await send(ctx, store, "hola, buen día")
    assert (result.outcome, result.order_id, result.evaluate) == ("no_order_data", None, False)
    assert await store_orders(ctx["session_factory"], store) == []


@pytest.mark.parametrize(
    ("text", "command", "outcome"),
    [
        ("sí", StoreCommand.CONFIRM, "ignored:nothing_to_confirm"),
        ("no", StoreCommand.REJECT, "ignored:nothing_to_reject"),
        ("ayuda", StoreCommand.HELP, "command"),
    ],
)
async def test_commands_skip_the_llm(
    ctx: dict[str, Any], text: str, command: StoreCommand, outcome: str
) -> None:
    store = await new_store(ctx["session_factory"])
    result = await send(ctx, store, text)
    assert (result.outcome, result.command, result.evaluate) == (outcome, command, False)
    assert ctx["order_extractor"].calls == []
    assert await store_orders(ctx["session_factory"], store) == []


# --- LLM failure -----------------------------------------------------------------------------


async def test_llm_failure_notifies_store_and_admins_and_keeps_the_draft(
    ctx: dict[str, Any],
) -> None:
    store = await new_store(ctx["session_factory"], name="Kiosco Sol")
    first = await send(ctx, store, WITHOUT_PAYMENT)
    ctx["order_extractor"] = StubExtractor(fail=True)

    result = await send(ctx, store, PAYMENT_ONLY, inbound_event_id=None, wa_message_id="WAM-x")

    assert (result.outcome, result.order_id, result.evaluate) == (
        "llm_failed",
        first.order_id,
        False,
    )
    async with ctx["session_factory"]() as session:
        rows = list(
            await session.scalars(
                select(OutboundMessage).where(OutboundMessage.id.in_(result.outbound_ids))
            )
        )
    by_template = sorted((r.template_key, r.recipient_jid) for r in rows)
    assert by_template == [
        (catalog.ADMIN_LLM_FAILURE, "5491100000001@s.whatsapp.net"),
        (catalog.ADMIN_LLM_FAILURE, "5491100000002@s.whatsapp.net"),
        (catalog.STORE_PROCESSING_ERROR, store.wa_jid),
    ]
    assert all("Kiosco Sol" in r.body for r in rows if r.template_key == catalog.ADMIN_LLM_FAILURE)
    order = await get_order(ctx["session_factory"], first.order_id)
    assert (order.status, order.payment_method) == ("draft", None)

    # The same message failing again (e.g. reprocessed) does not duplicate the alerts.
    again = await send(ctx, store, PAYMENT_ONLY, wa_message_id="WAM-x")
    assert again.outbound_ids == []


# --- races and idempotency ---------------------------------------------------------------------


async def test_concurrent_first_messages_share_one_draft(ctx: dict[str, Any]) -> None:
    """RN-09: messages processed at once for a store without a draft create exactly one."""
    store = await new_store(ctx["session_factory"])
    ctx["order_extractor"] = StubExtractor(ANSWERS, delay=0.05)
    texts = [CANONICAL_ORDER_TEXT, WITHOUT_PAYMENT, PAYMENT_ONLY, CORRECTION, CANONICAL_ORDER_TEXT]

    results = await asyncio.gather(*(send(ctx, store, t) for t in texts))

    orders = await store_orders(ctx["session_factory"], store)
    assert len(orders) == 1
    assert {r.order_id for r in results} == {orders[0].id}
    assert sorted(r.outcome for r in results) == ["draft_created"] + ["draft_updated"] * 4
    kinds = await events(ctx["session_factory"], orders[0].id)
    assert kinds.count("created") == 1 and kinds.count("draft_updated") == 4
    async with ctx["session_factory"]() as session:
        linked = await session.scalar(
            select(func.count()).select_from(Message).where(Message.order_id == orders[0].id)
        )
    assert linked == 5


async def test_concurrent_evaluations_send_one_summary(ctx: dict[str, Any]) -> None:
    store = await new_store(ctx["session_factory"])
    first = await send(ctx, store, CANONICAL_ORDER_TEXT)

    results = await asyncio.gather(*(evaluate(ctx, first.order_id) for _ in range(5)))

    outcomes = sorted(r.outcome for r in results)
    assert outcomes == ["confirmation_requested"] + ["skipped:awaiting_confirmation"] * 4
    assert len(await outbox(ctx["session_factory"], first.order_id)) == 1


async def test_concurrent_missing_field_evaluations_ask_once(ctx: dict[str, Any]) -> None:
    store = await new_store(ctx["session_factory"])
    first = await send(ctx, store, WITHOUT_PAYMENT)

    results = await asyncio.gather(*(evaluate(ctx, first.order_id) for _ in range(5)))

    assert (
        sorted(r.outcome for r in results) == ["missing_fields"] + ["skipped:already_evaluated"] * 4
    )
    assert len(await outbox(ctx["session_factory"], first.order_id)) == 1
    order = await get_order(ctx["session_factory"], first.order_id)
    assert order.missing_prompts_count == 1


async def test_debounce_postpones_evaluation(worker_ctx: Ctx) -> None:
    ctx = worker_ctx(draft_debounce_seconds=60)
    ctx["order_extractor"] = StubExtractor()
    store = await new_store(ctx["session_factory"])
    first = await send(ctx, store, CANONICAL_ORDER_TEXT)

    assert (await evaluate(ctx, first.order_id)).outcome == "debounced"
    assert await outbox(ctx["session_factory"], first.order_id) == []


async def test_evaluate_missing_order(ctx: dict[str, Any]) -> None:
    assert (await evaluate(ctx, 987654321)).outcome == "skipped:missing"


# --- jobs ------------------------------------------------------------------------------------


async def test_after_commit_defers_the_evaluation(worker_ctx: Ctx) -> None:
    ctx = worker_ctx(draft_debounce_seconds=8)
    ctx["order_extractor"] = StubExtractor()
    store = await new_store(ctx["session_factory"])
    result = await send(ctx, store, CANONICAL_ORDER_TEXT)

    await drafts.after_commit(ctx, result)

    jobs = await ctx["redis"].queued_jobs()
    [job] = [j for j in jobs if j.function == EVALUATE_DRAFT]
    assert job.args == (result.order_id,)
    assert job.score - job.enqueue_time.timestamp() * 1000 >= 7_000  # deferred ~8 s


async def test_evaluate_draft_job_enqueues_the_summary(ctx: dict[str, Any]) -> None:
    store = await new_store(ctx["session_factory"])
    result = await send(ctx, store, CANONICAL_ORDER_TEXT)

    assert await drafts.evaluate_draft(ctx, result.order_id) == "confirmation_requested"

    [summary] = await outbox(ctx["session_factory"], result.order_id)
    assert (SEND_OUTBOUND, (summary.id,)) in await queued(ctx["redis"], SEND_OUTBOUND)


# --- expiry (T09, CU-12) ---------------------------------------------------------------------


async def expire_now(session_factory: SF, order_id: int) -> None:
    async with session_factory() as session, session.begin():
        await session.execute(
            update(Order)
            .where(Order.id == order_id)
            .values(draft_expires_at=func.now() - dt.timedelta(seconds=1))
        )


@pytest.mark.parametrize("awaiting", [False, True])
async def test_expired_drafts_are_closed_and_the_store_told(
    ctx: dict[str, Any], awaiting: bool
) -> None:
    store = await new_store(ctx["session_factory"])
    first = await send(ctx, store, CANONICAL_ORDER_TEXT)
    if awaiting:
        await evaluate(ctx, first.order_id)
    fresh_store = await new_store(ctx["session_factory"])
    fresh = await send(ctx, fresh_store, CANONICAL_ORDER_TEXT)
    await expire_now(ctx["session_factory"], first.order_id)

    count = await drafts.expire_drafts(ctx)

    assert count >= 1
    order = await get_order(ctx["session_factory"], first.order_id)
    assert order.status == "expired"
    expired_msg = (await outbox(ctx["session_factory"], first.order_id))[-1]
    assert expired_msg.template_key == catalog.STORE_DRAFT_EXPIRED
    assert expired_msg.body == catalog.render(catalog.STORE_DRAFT_EXPIRED, minutes=30)
    assert (SEND_OUTBOUND, (expired_msg.id,)) in await queued(ctx["redis"], SEND_OUTBOUND)
    assert (await events(ctx["session_factory"], first.order_id))[-1] == "expired"
    assert (await get_order(ctx["session_factory"], fresh.order_id)).status == "draft"

    # Idempotent: a second sweep finds nothing for this order; a new message starts a new draft.
    await drafts.expire_drafts(ctx)
    assert len(await outbox(ctx["session_factory"], first.order_id)) == (2 if awaiting else 1)
    again = await send(ctx, store, CANONICAL_ORDER_TEXT)
    assert (again.outcome, again.order_id != first.order_id) == ("draft_created", True)


async def test_message_renewing_a_draft_wins_over_a_concurrent_expiry(
    ctx: dict[str, Any],
) -> None:
    """Reading the snapshot renews the expiry, so a sweep during the LLM call skips the draft."""
    store = await new_store(ctx["session_factory"])
    first = await send(ctx, store, WITHOUT_PAYMENT)
    await expire_now(ctx["session_factory"], first.order_id)
    ctx["order_extractor"] = StubExtractor(ANSWERS, delay=0.3)

    async def expire_later() -> list[int]:
        await asyncio.sleep(0.1)  # the message transaction already locked the draft
        async with ctx["session_factory"]() as session, session.begin():
            return await intake.expire_drafts(session, ctx["settings"])

    message, _ = await asyncio.gather(send(ctx, store, PAYMENT_ONLY), expire_later())

    assert message.outcome == "draft_updated"
    order = await get_order(ctx["session_factory"], first.order_id)
    assert (order.status, order.payment_method) == ("draft", "cash")
    assert await outbox(ctx["session_factory"], first.order_id) == []


# --- Redis store lock: no DB connection during the LLM call (docs/06) --------------------------


class PoolProbe(StubExtractor):
    """Sleeps like a slow LLM and tracks concurrency."""

    def __init__(self, delay: float) -> None:
        super().__init__(ANSWERS, delay=delay)
        self.running = 0
        self.max_running = 0

    async def extract(self, text: str, current: OrderExtraction | None = None) -> OrderExtraction:
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            return await super().extract(text, current)
        finally:
            self.running -= 1


async def test_slow_llm_does_not_hold_pool_connections(worker_ctx: Ctx, database_url: str) -> None:
    small = create_async_engine(database_url, pool_size=2, max_overflow=0, pool_timeout=2)
    try:
        ctx = worker_ctx(draft_debounce_seconds=0)
        ctx["session_factory"] = async_sessionmaker(small, expire_on_commit=False)
        probe = PoolProbe(delay=3.0)
        ctx["order_extractor"] = probe
        stores = [await new_store(ctx["session_factory"], f"Local {i}") for i in range(6)]

        async def other_db_work() -> tuple[int, float]:
            await asyncio.sleep(1.0)  # every store is inside its LLM call by now
            checked_out = small.pool.checkedout()
            started = time.monotonic()

            async def query() -> None:
                async with ctx["session_factory"]() as session:
                    assert await session.scalar(text("SELECT 1")) == 1

            await asyncio.gather(*(query() for _ in range(10)))
            return checked_out, time.monotonic() - started

        started = time.monotonic()
        *results, (checked_out, other_elapsed) = await asyncio.gather(
            *(send(ctx, store, CANONICAL_ORDER_TEXT) for store in stores), other_db_work()
        )
        elapsed = time.monotonic() - started

        assert [r.outcome for r in results] == ["draft_created"] * 6
        assert probe.max_running == 6  # 6 LLM calls at once with a pool of 2
        assert checked_out == 0  # no connection checked out while the LLM runs
        assert other_elapsed < 1.0  # other DB work proceeds during the LLM calls
        assert elapsed < 3.0 * 2  # stores ran in parallel; nobody waited for a connection
    finally:
        await small.dispose()


async def test_one_store_is_serialized_by_the_lock(ctx: dict[str, Any]) -> None:
    store = await new_store(ctx["session_factory"])
    probe = PoolProbe(delay=0.3)
    ctx["order_extractor"] = probe

    first, second = await asyncio.gather(
        send(ctx, store, WITHOUT_PAYMENT), send(ctx, store, PAYMENT_ONLY)
    )

    assert probe.max_running == 1
    # The later message saw the earlier one's draft as context.
    assert probe.calls[0][1] is None
    assert probe.calls[1][1] is not None
    assert sorted([first.outcome, second.outcome]) == ["draft_created", "draft_updated"]
    order = await get_order(ctx["session_factory"], first.order_id)
    assert (order.product, order.payment_method) == ("almacen", "cash")
    assert await ctx["redis"].get(store_lock_key(store.id)) is None


async def test_waits_for_a_held_store_lock(ctx: dict[str, Any]) -> None:
    store = await new_store(ctx["session_factory"])
    await ctx["redis"].set(store_lock_key(store.id), "other-worker", px=60_000)

    async def release_later() -> None:
        await asyncio.sleep(0.4)
        await ctx["redis"].delete(store_lock_key(store.id))

    started = time.monotonic()
    result, _ = await asyncio.gather(send(ctx, store, CANONICAL_ORDER_TEXT), release_later())

    assert result.outcome == "draft_created"
    assert time.monotonic() - started >= 0.4


async def test_lock_released_after_llm_failure(ctx: dict[str, Any]) -> None:
    store = await new_store(ctx["session_factory"])
    ctx["order_extractor"] = StubExtractor(fail=True)
    assert (await send(ctx, store, CANONICAL_ORDER_TEXT)).outcome == "llm_failed"
    assert await ctx["redis"].get(store_lock_key(store.id)) is None


class Meddler(StubExtractor):
    """Changes the draft behind the lock's back during the first ``times`` calls."""

    def __init__(self, session_factory: SF, store: Store, times: int) -> None:
        super().__init__(ANSWERS)
        self.session_factory = session_factory
        self.store = store
        self.times = times

    async def extract(self, text: str, current: OrderExtraction | None = None) -> OrderExtraction:
        result = await super().extract(text, current)
        if len(self.calls) <= self.times:
            async with self.session_factory() as session, session.begin():
                await session.execute(
                    update(Order)
                    .where(Order.store_id == self.store.id, Order.status == "draft")
                    .values(
                        product=f"meddled {len(self.calls)}",
                        last_store_message_at=func.clock_timestamp(),
                    )
                )
        return result


async def test_revision_changed_retries_with_the_latest_draft(ctx: dict[str, Any]) -> None:
    store = await new_store(ctx["session_factory"])
    first = await send(ctx, store, WITHOUT_PAYMENT)
    meddler = Meddler(ctx["session_factory"], store, times=1)
    ctx["order_extractor"] = meddler

    result = await send(ctx, store, PAYMENT_ONLY)

    assert (result.outcome, result.order_id) == ("draft_updated", first.order_id)
    assert len(meddler.calls) == 2
    assert meddler.calls[1][1].product == "meddled 1"  # re-read the latest draft
    order = await get_order(ctx["session_factory"], first.order_id)
    assert (order.product, order.payment_method) == ("meddled 1", "cash")
    assert (await events(ctx["session_factory"], order.id)).count("draft_updated") == 1


async def test_revision_retries_are_bounded(ctx: dict[str, Any]) -> None:
    store = await new_store(ctx["session_factory"])
    await send(ctx, store, WITHOUT_PAYMENT)
    meddler = Meddler(ctx["session_factory"], store, times=99)
    ctx["order_extractor"] = meddler

    with pytest.raises(intake.DraftChangedError):
        await send(ctx, store, PAYMENT_ONLY)
    assert len(meddler.calls) == intake.MAX_APPLY_ATTEMPTS
    assert await ctx["redis"].get(store_lock_key(store.id)) is None


async def test_draft_created_elsewhere_during_extraction_is_merged(ctx: dict[str, Any]) -> None:
    store = await new_store(ctx["session_factory"])

    class CreatesDraft(StubExtractor):
        async def extract(self, t: str, current: OrderExtraction | None = None) -> OrderExtraction:
            result = await super().extract(t, current)
            if len(self.calls) == 1:
                async with ctx["session_factory"]() as session, session.begin():
                    await f.insert_row(
                        await session.connection(),
                        f.order(store.id, product="otro", last_store_message_at=f.NOW),
                    )
            return result

    ctx["order_extractor"] = CreatesDraft(ANSWERS)
    result = await send(ctx, store, PAYMENT_ONLY)

    assert result.outcome == "draft_updated"
    [order] = await store_orders(ctx["session_factory"], store)
    assert (order.id, order.product, order.payment_method) == (result.order_id, "otro", "cash")


async def test_concurrent_creation_keeps_one_draft_without_the_lock(ctx: dict[str, Any]) -> None:
    """RN-09 fallback: two apply transactions that both saw no draft create exactly one."""
    store = await new_store(ctx["session_factory"])
    snapshot = intake.DraftSnapshot(store.id, None, None, None)
    extraction = ANSWERS[WITHOUT_PAYMENT]

    async def apply(i: int) -> intake.IntakeResult | None:
        async with ctx["session_factory"]() as session, session.begin():
            row = await session.get(Store, store.id)
            assert row is not None
            msg = StoreText(store_id=store.id, text=WITHOUT_PAYMENT, wa_message_id=f"W{i}")
            result = await intake._apply(session, ctx["settings"], row, msg, snapshot, extraction)
            await asyncio.sleep(0.2)  # keep the winner's insert uncommitted for a while
            return result

    results = await asyncio.gather(*(apply(i) for i in range(4)))

    assert sum(r is not None for r in results) == 1  # the others must re-read and retry
    assert len(await store_orders(ctx["session_factory"], store)) == 1
