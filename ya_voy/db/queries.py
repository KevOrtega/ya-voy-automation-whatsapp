"""Data-access helpers for the guarantees the database itself must provide.

Only what docs/05 defines at the database level lives here (stage 2): order codes from
``order_code_seq``, webhook dedup on ``inbound_events.wa_message_id`` (RF-59) and outbox
idempotency on ``outbound_messages.idempotency_key`` (RF-60, D-03). Business logic comes later.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ya_voy.db.models import InboundEvent, OutboundMessage, order_code_seq
from ya_voy.domain.enums import OutboundMessageType

ORDER_CODE_DIGITS = 4


def format_order_code(prefix: str, number: int) -> str:
    """``YV-`` + 42 -> ``YV-0042``. Numbers past 9999 keep growing (``YV-10000``)."""
    return f"{prefix}{number:0{ORDER_CODE_DIGITS}d}"


async def next_order_code(session: AsyncSession, prefix: str) -> str:
    """Next published-order code. ``nextval`` is never rolled back, so gaps are possible."""
    number = await session.scalar(select(func.nextval(order_code_seq.name)))
    assert number is not None
    return format_order_code(prefix, number)


async def insert_inbound_event(
    session: AsyncSession,
    *,
    wa_message_id: str,
    instance: str,
    event_type: str,
    payload: dict[str, Any],
    remote_jid: str | None = None,
    participant: str | None = None,
) -> int | None:
    """Store a webhook event once. Returns its id, or ``None`` if it was a duplicate (CU-11).

    Only enqueue processing when an id comes back.
    """
    stmt = (
        insert(InboundEvent)
        .values(
            wa_message_id=wa_message_id,
            instance=instance,
            event_type=event_type,
            remote_jid=remote_jid,
            participant=participant,
            payload=payload,
        )
        .on_conflict_do_nothing(index_elements=[InboundEvent.wa_message_id])
        .returning(InboundEvent.id)
    )
    return await session.scalar(stmt)


async def enqueue_outbound_message(
    session: AsyncSession,
    *,
    idempotency_key: str,
    template_key: str,
    recipient_jid: str,
    message_type: OutboundMessageType = OutboundMessageType.TEXT,
    body: str = "",
    order_id: int | None = None,
    report_revision_id: int | None = None,
    quoted_wa_message_id: str | None = None,
    target_wa_message_id: str | None = None,
) -> int | None:
    """Add a message to the outbox once per ``idempotency_key``.

    Call it in the same transaction as the state change. Returns the new id, or ``None`` if a
    message with that key already exists (nothing is changed then).
    """
    stmt = (
        insert(OutboundMessage)
        .values(
            idempotency_key=idempotency_key,
            template_key=template_key,
            recipient_jid=recipient_jid,
            message_type=str(message_type),
            body=body,
            order_id=order_id,
            report_revision_id=report_revision_id,
            quoted_wa_message_id=quoted_wa_message_id,
            target_wa_message_id=target_wa_message_id,
        )
        .on_conflict_do_nothing(index_elements=[OutboundMessage.idempotency_key])
        .returning(OutboundMessage.id)
    )
    return await session.scalar(stmt)
