"""Order intake for a store that is already identified (CU-01, CU-02, T01-T05, T09).

Everything here works on a normalized message and a known ``Store`` row. Who the sender is
(admin / courier / store), store auto-registration and ``@lid`` handling are stage 4b
(``ya_voy/identity``); see ``TODO(stage-4b)`` in ``ya_voy/worker/inbound.py``.

Concurrency (RN-09, ``orders_one_draft_per_store``): the open draft is locked with
``SELECT ... FOR UPDATE``; when there is none, the draft is created with
``INSERT ... ON CONFLICT DO NOTHING`` on that partial unique index, and the loser of a concurrent
insert locks the winner's row and merges into it. No Redis lock is needed for correctness.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

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
from ya_voy.messaging import catalog
from ya_voy.messaging.outbox import enqueue_admin_alert
from ya_voy.schedule.calendar import business_date

logger = logging.getLogger(__name__)

_IN_PREPARATION = [str(s) for s in IN_PREPARATION_STATUSES]


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


async def handle_store_text(
    session: AsyncSession,
    settings: Settings,
    extractor: OrderExtractor,
    msg: StoreText,
) -> IntakeResult:
    """T01 / T02 / T05 for one text message. Call inside a transaction; commit, then enqueue.

    TODO(stage-4b): ``is_accepting_orders(now)`` guard (RN-18, CU-10) before touching drafts.
    """
    store = await session.get(Store, msg.store_id)
    if store is None:
        raise ValueError(f"store {msg.store_id} does not exist")

    command = parse_store_command(msg.text)
    if command is not None:
        # TODO(stage-4b): CONFIRM -> T06/T07, REJECT/CANCEL -> T08 (or CU-09 for published
        # orders), HELP -> store_help. Commands never reach the LLM (CU-01 5a).
        _record_message(session, store, msg, None)
        return IntakeResult("command", command=command)

    now = await _now(session)
    draft = await _lock_open_draft(session, store.id)
    current = fields_of(draft) if draft is not None else None
    try:
        extraction = await extractor.extract(
            msg.text, current.as_extraction() if current is not None else None
        )
    except LLMError:
        logger.warning("order extraction failed", extra={"store_id": store.id}, exc_info=True)
        _record_message(session, store, msg, draft.id if draft else None)
        return await _llm_failed(session, settings, store, msg, draft.id if draft else None)

    created = False
    if draft is None:
        if clean_extraction(extraction).is_empty:
            # CU-01 6b: nothing to start a draft with.
            # TODO(stage-4b): store_help at most once per HELP_REPLY_COOLDOWN_MINUTES.
            _record_message(session, store, msg, None)
            return IntakeResult("no_order_data")
        new_id = await _create_draft(session, settings, store.id, now)
        created = new_id is not None
        # Lock our new row, or the concurrent winner's once it commits.
        draft = await _lock_open_draft(session, store.id)
        assert draft is not None, "the draft must exist after the insert"

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
