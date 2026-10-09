"""Order intake for a store that is already identified (CU-01, CU-02, T01-T05, T09).

Everything here works on a normalized message and a known ``Store`` row. Who the sender is
(admin / courier / store), store auto-registration and ``@lid`` handling are stage 4b
(``ya_voy/identity``); see ``TODO(stage-4b)`` in ``ya_voy/worker/inbound.py``.

Concurrency: a store's messages are serialized by a per-store Redis lock whose TTL exceeds the
worst-case LLM time (``ya_voy.locks``). The LLM runs with no database connection checked out,
between a short snapshot transaction and a short apply transaction. The apply transaction locks the
open draft (``SELECT ... FOR UPDATE``) and checks its revision (``last_store_message_at``); a
mismatch, possible only if the lock was lost, re-reads and retries (bounded). Draft creation keeps
``INSERT ... ON CONFLICT DO NOTHING`` on ``orders_one_draft_per_store`` (RN-09).
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ya_voy.config import Settings
from ya_voy.db.models import Message, Order, OrderEvent, Store
from ya_voy.db.queries import enqueue_outbound_message
from ya_voy.domain.commands import StoreCommand, parse_store_command
from ya_voy.domain.enums import (
    IN_PREPARATION_STATUSES,
    ActorType,
    MessageDirection,
    MessageKind,
    OrderEventType,
    OrderStatus,
    PaymentMethod,
)
from ya_voy.domain.orders import OrderFields, clean_extraction, merge, validate_required_fields
from ya_voy.llm.extractor import LLMError, OrderExtractor
from ya_voy.llm.schemas import OrderExtraction
from ya_voy.locks import store_lock
from ya_voy.messaging import catalog
from ya_voy.messaging.outbox import enqueue_admin_alert
from ya_voy.schedule.calendar import business_date

logger = logging.getLogger(__name__)

_IN_PREPARATION = [str(s) for s in IN_PREPARATION_STATUSES]
# Snapshot -> LLM -> apply rounds per message; more than one only if the store lock was lost.
MAX_APPLY_ATTEMPTS = 3


@dataclass(frozen=True)
class StoreText:
    """A private text message from an identified store."""

    store_id: int
    text: str
    inbound_event_id: int | None = None
    wa_message_id: str | None = None
    quoted_wa_message_id: str | None = None


@dataclass
class IntakeResult:
    outcome: str
    order_id: int | None = None
    outbound_ids: list[int] = field(default_factory=list)
    # Enqueue ``evaluate_draft(order_id)`` with ``DRAFT_DEBOUNCE_SECONDS`` after the commit.
    evaluate: bool = False
    command: StoreCommand | None = None


def fields_of(order: Order) -> OrderFields:
    return OrderFields(
        pickup_address=order.pickup_address,
        delivery_address=order.delivery_address,
        product=order.product,
        payment_method=PaymentMethod(order.payment_method) if order.payment_method else None,
        payment_method_raw=order.payment_method_raw,
        notes=order.notes,
    )


async def _now(session: AsyncSession) -> dt.datetime:
    now = await session.scalar(select(func.now()))
    assert now is not None
    return now


async def _lock_open_draft(session: AsyncSession, store_id: int) -> Order | None:
    return await session.scalar(
        select(Order)
        .where(Order.store_id == store_id, Order.status.in_(_IN_PREPARATION))
        .with_for_update()
    )


async def _create_draft(
    session: AsyncSession, settings: Settings, store_id: int, now: dt.datetime
) -> int | None:
    """T01 insert; ``None`` if another transaction holds the store's draft (RN-09)."""
    stmt = (
        insert(Order)
        .values(
            store_id=store_id,
            status=OrderStatus.DRAFT,
            business_date=business_date(
                now, settings.zoneinfo, settings.business_day_rollover_time
            ),
        )
        .on_conflict_do_nothing(
            index_elements=[Order.store_id],
            index_where=text("status IN ('draft', 'awaiting_confirmation')"),
        )
        .returning(Order.id)
    )
    return await session.scalar(stmt)


def _record_message(session: AsyncSession, store: Store, msg: StoreText, order_id: int | None):
    session.add(
        Message(
            direction=MessageDirection.IN,
            chat_jid=store.wa_jid,
            sender_jid=store.wa_jid,
            wa_message_id=msg.wa_message_id,
            quoted_wa_message_id=msg.quoted_wa_message_id,
            kind=MessageKind.TEXT,
            body=msg.text,
            store_id=store.id,
            order_id=order_id,
            inbound_event_id=msg.inbound_event_id,
        )
    )


def _event(
    order_id: int,
    event_type: OrderEventType,
    store_id: int,
    *,
    from_status: str | None,
    to_status: str | None,
    inbound_event_id: int | None,
    data: dict[str, Any] | None = None,
) -> OrderEvent:
    return OrderEvent(
        order_id=order_id,
        event_type=event_type,
        actor_type=ActorType.STORE,
        actor_id=store_id,
        from_status=from_status,
        to_status=to_status,
        inbound_event_id=inbound_event_id,
        data=data or {},
    )


async def _llm_failed(
    session: AsyncSession, settings: Settings, store: Store, msg: StoreText, order_id: int | None
) -> IntakeResult:
    """CU-01 6a: tell the store and the admins; the draft (if any) is kept unchanged."""
    key = f"inbound:{msg.inbound_event_id}" if msg.inbound_event_id else f"msg:{msg.wa_message_id}"
    ids = []
    store_id = await enqueue_outbound_message(
        session,
        idempotency_key=f"{catalog.STORE_PROCESSING_ERROR}:{key}",
        template_key=catalog.STORE_PROCESSING_ERROR,
        recipient_jid=store.wa_jid,
        body=catalog.render(catalog.STORE_PROCESSING_ERROR),
        order_id=order_id,
    )
    if store_id is not None:
        ids.append(store_id)
    ids += await enqueue_admin_alert(
        session,
        settings,
        template_key=catalog.ADMIN_LLM_FAILURE,
        idempotency_base=f"{catalog.ADMIN_LLM_FAILURE}:{key}",
        variables={"store_name": store.display_name},
    )
    return IntakeResult("llm_failed", order_id=order_id, outbound_ids=ids)


@dataclass(frozen=True)
class DraftSnapshot:
    """What the LLM call is based on, read in a short transaction that is already closed."""

    store_id: int
    order_id: int | None
    revision: dt.datetime | None
    current: OrderExtraction | None


class DraftChangedError(Exception):
    """The draft kept changing under the extraction (lock lost repeatedly); event fails."""


Finalize = Callable[[AsyncSession], Awaitable[None]]


async def _read_snapshot(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings, store_id: int
) -> DraftSnapshot:
    """Short transaction: the open draft and its revision; also renews its expiry.

    Renewing here (not after the LLM) keeps ``expire_drafts`` from closing a draft whose store
    message is being extracted. The revision (``last_store_message_at``) is not touched.
    """
    async with session_factory() as session, session.begin():
        if await session.get(Store, store_id) is None:
            raise ValueError(f"store {store_id} does not exist")
        draft = await _lock_open_draft(session, store_id)
        if draft is None:
            return DraftSnapshot(store_id, None, None, None)
        now = await _now(session)
        renewed = now + dt.timedelta(minutes=settings.draft_ttl_minutes)
        if draft.draft_expires_at is None or draft.draft_expires_at < renewed:
            draft.draft_expires_at = renewed
        return DraftSnapshot(
            store_id, draft.id, draft.last_store_message_at, fields_of(draft).as_extraction()
        )


async def _apply(
    session: AsyncSession,
    settings: Settings,
    store: Store,
    msg: StoreText,
    snapshot: DraftSnapshot,
    extraction: OrderExtraction,
) -> IntakeResult | None:
    """T01 / T02 / T05 with the extraction; ``None`` if the draft changed since the snapshot."""
    now = await _now(session)
    draft = await _lock_open_draft(session, store.id)
    created = False
    if snapshot.order_id is None:
        if draft is not None:
            return None
        if clean_extraction(extraction).is_empty:
            # CU-01 6b: nothing to start a draft with.
            # TODO(stage-4b): store_help at most once per HELP_REPLY_COOLDOWN_MINUTES.
            _record_message(session, store, msg, None)
            return IntakeResult("no_order_data")
        if await _create_draft(session, settings, store.id, now) is None:
            return None  # another transaction created it (RN-09): re-read and retry
        created = True
        draft = await _lock_open_draft(session, store.id)
        assert draft is not None, "the draft must exist after the insert"
    elif (
        draft is None
        or draft.id != snapshot.order_id
        or draft.last_store_message_at != snapshot.revision
    ):
        return None

    from_status = draft.status
    merged = merge(fields_of(draft), extraction)
    for column, value in merged.as_columns().items():
        setattr(draft, column, value)
    draft.extraction = merged.as_extraction().model_dump(mode="json")
    draft.last_store_message_at = now
    draft.draft_expires_at = now + dt.timedelta(minutes=settings.draft_ttl_minutes)
    draft.updated_at = now
    if draft.status == OrderStatus.AWAITING_CONFIRMATION:
        # T05: any correction invalidates the summary that was sent (RN-15).
        draft.status = OrderStatus.DRAFT

    if created:
        event_type, outcome = OrderEventType.CREATED, "draft_created"
    else:
        event_type = OrderEventType.DRAFT_UPDATED
        outcome = (
            "correction" if from_status == OrderStatus.AWAITING_CONFIRMATION else "draft_updated"
        )
    session.add(
        _event(
            draft.id,
            event_type,
            store.id,
            from_status=None if created else from_status,
            to_status=draft.status,
            inbound_event_id=msg.inbound_event_id,
            data={"extraction": extraction.model_dump(mode="json", exclude_none=True)},
        )
    )
    _record_message(session, store, msg, draft.id)
    return IntakeResult(outcome, order_id=draft.id, evaluate=True)


async def handle_store_text(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    settings: Settings,
    extractor: OrderExtractor,
    msg: StoreText,
    *,
    finalize: Finalize | None = None,
) -> IntakeResult:
    """T01 / T02 / T05 for one text message; commits on its own, then the caller enqueues.

    Holds the store's Redis lock (``ya_voy.locks``) for the whole message, but no database
    connection while the LLM runs: snapshot transaction, LLM call, apply transaction. The apply
    step checks the draft revision and, if it changed (only possible if the lock was lost),
    retries from a fresh snapshot up to ``MAX_APPLY_ATTEMPTS`` times. ``finalize`` runs inside
    the transaction that records the outcome (stage 4b marks the inbound event processed there).

    TODO(stage-4b): ``is_accepting_orders(now)`` guard (RN-18, CU-10) before touching drafts.
    """
    async with store_lock(redis, settings, msg.store_id) as lock:
        command = parse_store_command(msg.text)
        if command is not None:
            # TODO(stage-4b): CONFIRM -> T06/T07, REJECT/CANCEL -> T08 (or CU-09 for published
            # orders), HELP -> store_help. Commands never reach the LLM (CU-01 5a).
            async with session_factory() as session, session.begin():
                store = await _get_store(session, msg.store_id)
                _record_message(session, store, msg, None)
                if finalize is not None:
                    await finalize(session)
            return IntakeResult("command", command=command)

        for attempt in range(1, MAX_APPLY_ATTEMPTS + 1):
            snapshot = await _read_snapshot(session_factory, settings, msg.store_id)
            await lock.refresh()
            try:
                # No database connection is held here (RNF: pool sized for short transactions).
                extraction = await extractor.extract(msg.text, snapshot.current)
            except LLMError:
                logger.warning(
                    "order extraction failed", extra={"store_id": msg.store_id}, exc_info=True
                )
                async with session_factory() as session, session.begin():
                    store = await _get_store(session, msg.store_id)
                    _record_message(session, store, msg, snapshot.order_id)
                    result = await _llm_failed(session, settings, store, msg, snapshot.order_id)
                    if finalize is not None:
                        await finalize(session)
                return result

            async with session_factory() as session, session.begin():
                store = await _get_store(session, msg.store_id)
                result = await _apply(session, settings, store, msg, snapshot, extraction)
                if result is not None:
                    if finalize is not None:
                        await finalize(session)
                    return result
            logger.warning(
                "draft changed during extraction, retrying",
                extra={"store_id": msg.store_id, "attempt": attempt},
            )
    raise DraftChangedError(f"store {msg.store_id}: draft changed {MAX_APPLY_ATTEMPTS} times")


async def _get_store(session: AsyncSession, store_id: int) -> Store:
    store = await session.get(Store, store_id)
    if store is None:
        raise ValueError(f"store {store_id} does not exist")
    return store


@dataclass
class EvaluationResult:
    outcome: str
    outbound_ids: list[int] = field(default_factory=list)


def _revision(order: Order) -> str:
    """Identifies the draft content being evaluated (one evaluation per store message)."""
    stamp = order.last_store_message_at or order.created_at
    return stamp.isoformat()


async def evaluate_draft(
    session: AsyncSession, settings: Settings, order_id: int
) -> EvaluationResult:
    """T03 / T04 after the debounce (RF-15, RF-16, RF-17). Call inside a transaction.

    Safe to run twice: only a ``draft`` is evaluated, a newer store message postpones it (its own
    job evaluates later), and the outbox keys include the draft revision.
    """
    order = await session.scalar(select(Order).where(Order.id == order_id).with_for_update())
    if order is None:
        return EvaluationResult("skipped:missing")
    if order.status != OrderStatus.DRAFT:
        return EvaluationResult(f"skipped:{order.status}")
    now = await _now(session)
    debounce = dt.timedelta(seconds=settings.draft_debounce_seconds)
    if order.last_store_message_at and now < order.last_store_message_at + debounce:
        return EvaluationResult("debounced")

    store = await session.get(Store, order.store_id)
    assert store is not None
    fields = fields_of(order)
    issues = validate_required_fields(fields)
    revision = _revision(order)

    if issues:
        missing_key = f"order:{order.id}:missing_fields:{revision}"
        new_id = await enqueue_outbound_message(
            session,
            idempotency_key=missing_key,
            template_key=catalog.STORE_ORDER_MISSING_FIELDS,
            recipient_jid=store.wa_jid,
            body=catalog.render(
                catalog.STORE_ORDER_MISSING_FIELDS,
                missing_list=catalog.missing_list([i.field for i in issues]),
            ),
            order_id=order.id,
        )
        if new_id is None:
            return EvaluationResult("skipped:already_evaluated")
        ids = [new_id]
        order.missing_prompts_count += 1
        order.validation_errors = [i.as_json() for i in issues]
        order.updated_at = now
        if order.missing_prompts_count >= settings.max_missing_fields_prompts:
            example_id = await enqueue_outbound_message(
                session,
                idempotency_key=f"order:{order.id}:format_example:{revision}",
                template_key=catalog.STORE_ORDER_FORMAT_EXAMPLE,
                recipient_jid=store.wa_jid,
                body=catalog.render(catalog.STORE_ORDER_FORMAT_EXAMPLE),
                order_id=order.id,
            )
            if example_id is not None:
                ids.append(example_id)
        session.add(
            _event(
                order.id,
                OrderEventType.MISSING_FIELDS_REQUESTED,
                store.id,
                from_status=order.status,
                to_status=order.status,
                inbound_event_id=None,
                data={"missing": [i.field for i in issues]},
            )
        )
        return EvaluationResult("missing_fields", ids)

    summary_id = await enqueue_outbound_message(
        session,
        idempotency_key=f"order:{order.id}:confirm_request:{revision}",
        template_key=catalog.STORE_ORDER_CONFIRM_REQUEST,
        recipient_jid=store.wa_jid,
        body=render_confirmation_summary(order, store),
        order_id=order.id,
    )
    if summary_id is None:
        return EvaluationResult("skipped:already_evaluated")
    order.status = OrderStatus.AWAITING_CONFIRMATION  # T04
    order.validation_errors = []
    order.confirmation_requested_at = now
    order.draft_expires_at = now + dt.timedelta(minutes=settings.draft_ttl_minutes)
    order.updated_at = now
    session.add(
        _event(
            order.id,
            OrderEventType.CONFIRMATION_REQUESTED,
            store.id,
            from_status=OrderStatus.DRAFT,
            to_status=OrderStatus.AWAITING_CONFIRMATION,
            inbound_event_id=None,
        )
    )
    return EvaluationResult("confirmation_requested", [summary_id])


def render_confirmation_summary(order: Order, store: Store) -> str:
    """``store_order_confirm_request`` with the ``{order_details}`` block (doc 07)."""
    details = catalog.order_details(
        store_name=store.display_name,
        pickup_address=order.pickup_address,
        delivery_address=order.delivery_address,
        product=order.product,
        payment_method=order.payment_method,
        payment_method_raw=order.payment_method_raw,
        notes=order.notes,
    )
    return catalog.render(catalog.STORE_ORDER_CONFIRM_REQUEST, order_details=details)


async def expire_drafts(session: AsyncSession, settings: Settings) -> list[int]:
    """T09 / CU-12: drafts and unconfirmed orders past ``draft_expires_at``.

    The conditional ``UPDATE`` is the guard: a concurrent store message that renewed the draft
    (it holds the row lock) makes the row no longer match. Returns the outbox ids to enqueue.
    """
    rows = (
        await session.execute(
            update(Order)
            .where(Order.status.in_(_IN_PREPARATION), Order.draft_expires_at < func.now())
            .values(status=OrderStatus.EXPIRED, updated_at=func.now())
            .returning(Order.id, Order.store_id)
        )
    ).all()
    ids: list[int] = []
    for order_id, store_id in rows:
        store = await session.get(Store, store_id)
        assert store is not None
        session.add(
            OrderEvent(
                order_id=order_id,
                event_type=OrderEventType.EXPIRED,
                actor_type=ActorType.SYSTEM,
                to_status=OrderStatus.EXPIRED,
            )
        )
        new_id = await enqueue_outbound_message(
            session,
            idempotency_key=f"order:{order_id}:expired",
            template_key=catalog.STORE_DRAFT_EXPIRED,
            recipient_jid=store.wa_jid,
            body=catalog.render(catalog.STORE_DRAFT_EXPIRED, minutes=settings.draft_ttl_minutes),
            order_id=order_id,
        )
        if new_id is not None:
            ids.append(new_id)
    return ids
