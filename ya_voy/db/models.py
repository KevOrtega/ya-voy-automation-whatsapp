"""SQLAlchemy models: one class per table in docs/05-modelo-datos.md.

Names, types, nullability, defaults, CHECKs, uniques and indexes follow that document verbatim.
Timestamps are ``timestamptz`` (UTC); primary keys are ``bigint generated always as identity``.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    Sequence,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ya_voy.db.base import Base
from ya_voy.domain.enums import (
    IN_PREPARATION_STATUSES,
    PENDING_STATUSES,
    ActorType,
    CancelledBy,
    DailyReportStatus,
    InboundEventStatus,
    MessageDirection,
    OrderStatus,
    OutboundMessageType,
    OutboundStatus,
    PaymentMethod,
    ReportRevisionKind,
    ReportRevisionStatus,
    ScheduleOverrideKind,
    ScheduleOverrideStatus,
)

# Used for orders.code at publish time: ORDER_CODE_PREFIX + zero-padded nextval (YV-0042).
order_code_seq = Sequence("order_code_seq", metadata=Base.metadata)


def sql_in(column: str, values: type[StrEnum] | tuple[StrEnum, ...]) -> str:
    """``column IN ('a', 'b')`` from enum members (fixed identifiers, never user input)."""
    return f"{column} IN ({', '.join(repr(str(v)) for v in values)})"


def _pk() -> Mapped[int]:
    return mapped_column(BigInteger, Identity(always=True), primary_key=True)


def _ts(*, nullable: bool = True) -> Any:
    return mapped_column(DateTime(timezone=True), nullable=nullable)


def _ts_now(*, onupdate: bool = False) -> Any:
    return mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now() if onupdate else None,
    )


def _jsonb(default: str | None) -> Any:
    # none_as_null: Python None is sent as SQL NULL (rejected by NOT NULL), never as JSON 'null'.
    return mapped_column(
        JSONB(none_as_null=True),
        nullable=False,
        server_default=text(f"'{default}'::jsonb") if default is not None else None,
    )


def _int0() -> Mapped[int]:
    return mapped_column(Integer, nullable=False, server_default=text("0"))


def _fk(target: str, *, nullable: bool = True) -> Any:
    return mapped_column(BigInteger, ForeignKey(target), nullable=nullable)


class Store(Base):
    __tablename__ = "stores"

    id: Mapped[int] = _pk()
    wa_jid: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    phone: Mapped[str | None] = mapped_column(Text, unique=True)
    lid: Mapped[str | None] = mapped_column(Text, unique=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    is_blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    last_message_at: Mapped[dt.datetime | None] = _ts()
    out_of_hours_replied_at: Mapped[dt.datetime | None] = _ts()
    created_at: Mapped[dt.datetime] = _ts_now()
    updated_at: Mapped[dt.datetime] = _ts_now(onupdate=True)


class Courier(Base):
    __tablename__ = "couriers"
    __table_args__ = (
        CheckConstraint("lid IS NOT NULL OR phone_jid IS NOT NULL", name="has_identity"),
    )

    id: Mapped[int] = _pk()
    lid: Mapped[str | None] = mapped_column(Text, unique=True)
    phone_jid: Mapped[str | None] = mapped_column(Text, unique=True)
    phone: Mapped[str | None] = mapped_column(Text, unique=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    is_blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    first_seen_at: Mapped[dt.datetime] = _ts_now()
    last_seen_at: Mapped[dt.datetime] = _ts_now()
    updated_at: Mapped[dt.datetime] = _ts_now(onupdate=True)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint(sql_in("status", OrderStatus), name="status_check"),
        CheckConstraint(sql_in("payment_method", PaymentMethod), name="payment_method_check"),
        CheckConstraint(sql_in("cancelled_by", CancelledBy), name="cancelled_by_check"),
        # courier_id is set only while assigned
        CheckConstraint(
            "(status = 'assigned') = (courier_id IS NOT NULL)", name="courier_matches_status"
        ),
        # Every order that reached the group has a code
        CheckConstraint("published_at IS NULL OR code IS NOT NULL", name="code_when_published"),
        # At most one order in preparation per store (RN-09)
        Index(
            "orders_one_draft_per_store",
            "store_id",
            unique=True,
            postgresql_where=text(sql_in("status", IN_PREPARATION_STATUSES)),
        ),
        Index(
            "orders_unclaimed_alert_idx",
            "unclaimed_alert_at",
            postgresql_where=text("status = 'published' AND unclaimed_alert_sent_at IS NULL"),
        ),
        Index(
            "orders_draft_expires_at_idx",
            "draft_expires_at",
            postgresql_where=text(sql_in("status", IN_PREPARATION_STATUSES)),
        ),
        Index(
            "orders_assigned_courier_id_idx",
            "courier_id",
            postgresql_where=text("status = 'assigned'"),
        ),
        Index(
            "orders_pending_business_date_idx",
            "business_date",
            postgresql_where=text(sql_in("status", PENDING_STATUSES)),
        ),
        Index("orders_business_date_idx", "business_date"),
    )

    id: Mapped[int] = _pk()
    code: Mapped[str | None] = mapped_column(Text, unique=True)
    store_id: Mapped[int] = _fk("stores.id", nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    business_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    courier_id: Mapped[int | None] = _fk("couriers.id")
    last_courier_id: Mapped[int | None] = _fk("couriers.id")
    pickup_address: Mapped[str | None] = mapped_column(Text)
    delivery_address: Mapped[str | None] = mapped_column(Text)
    product: Mapped[str | None] = mapped_column(Text)
    payment_method: Mapped[str | None] = mapped_column(Text)
    payment_method_raw: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    extraction: Mapped[dict[str, Any]] = _jsonb("{}")
    validation_errors: Mapped[list[dict[str, Any]]] = _jsonb("[]")
    missing_prompts_count: Mapped[int] = _int0()
    draft_expires_at: Mapped[dt.datetime | None] = _ts()
    last_store_message_at: Mapped[dt.datetime | None] = _ts()
    confirmation_requested_at: Mapped[dt.datetime | None] = _ts()
    confirmed_at: Mapped[dt.datetime | None] = _ts()
    group_message_id: Mapped[str | None] = mapped_column(Text, unique=True)
    group_post_deleted_at: Mapped[dt.datetime | None] = _ts()
    published_at: Mapped[dt.datetime | None] = _ts()
    unclaimed_alert_at: Mapped[dt.datetime | None] = _ts()
    unclaimed_alert_sent_at: Mapped[dt.datetime | None] = _ts()
    assigned_at: Mapped[dt.datetime | None] = _ts()
    cancelled_at: Mapped[dt.datetime | None] = _ts()
    cancelled_by: Mapped[str | None] = mapped_column(Text)
    anonymized_at: Mapped[dt.datetime | None] = _ts()
    created_at: Mapped[dt.datetime] = _ts_now()
    updated_at: Mapped[dt.datetime] = _ts_now(onupdate=True)


Index("orders_store_id_created_at_idx", Order.store_id, Order.created_at.desc())


class OrderEvent(Base):
    __tablename__ = "order_events"
    __table_args__ = (
        CheckConstraint(sql_in("actor_type", ActorType), name="actor_type_check"),
        Index("order_events_order_id_created_at_idx", "order_id", "created_at"),
        Index("order_events_created_at_courier_id_idx", "created_at", "courier_id"),
    )

    id: Mapped[int] = _pk()
    order_id: Mapped[int] = _fk("orders.id", nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[int | None] = mapped_column(BigInteger)
    courier_id: Mapped[int | None] = _fk("couriers.id")
    from_status: Mapped[str | None] = mapped_column(Text)
    to_status: Mapped[str | None] = mapped_column(Text)
    inbound_event_id: Mapped[int | None] = _fk("inbound_events.id")
    data: Mapped[dict[str, Any]] = _jsonb("{}")
    created_at: Mapped[dt.datetime] = _ts_now()


class InboundEvent(Base):
    __tablename__ = "inbound_events"
    __table_args__ = (
        CheckConstraint(sql_in("status", InboundEventStatus), name="status_check"),
        Index(
            "inbound_events_recovery_idx",
            "status",
            "received_at",
            postgresql_where=text("status IN ('received', 'failed')"),
        ),
    )

    id: Mapped[int] = _pk()
    wa_message_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    instance: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    remote_jid: Mapped[str | None] = mapped_column(Text)
    participant: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = _jsonb(None)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'received'"))
    attempts: Mapped[int] = _int0()
    error: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[dt.datetime] = _ts_now()
    processed_at: Mapped[dt.datetime | None] = _ts()


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint(sql_in("direction", MessageDirection), name="direction_check"),
        # The originating webhook event only exists for incoming messages, the outbox row only
        # for outgoing ones (doc 05 does not make them mandatory).
        CheckConstraint(
            "direction = 'in' OR inbound_event_id IS NULL", name="inbound_event_only_in"
        ),
        CheckConstraint(
            "direction = 'out' OR outbound_message_id IS NULL", name="outbound_message_only_out"
        ),
        Index("messages_chat_jid_created_at_idx", "chat_jid", "created_at"),
        Index("messages_order_id_idx", "order_id"),
        Index("messages_wa_message_id_idx", "wa_message_id"),
    )

    id: Mapped[int] = _pk()
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    chat_jid: Mapped[str] = mapped_column(Text, nullable=False)
    sender_jid: Mapped[str | None] = mapped_column(Text)
    wa_message_id: Mapped[str | None] = mapped_column(Text)
    quoted_wa_message_id: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    store_id: Mapped[int | None] = _fk("stores.id")
    courier_id: Mapped[int | None] = _fk("couriers.id")
    order_id: Mapped[int | None] = _fk("orders.id")
    inbound_event_id: Mapped[int | None] = _fk("inbound_events.id")
    outbound_message_id: Mapped[int | None] = _fk("outbound_messages.id")
    created_at: Mapped[dt.datetime] = _ts_now()


class OutboundMessage(Base):
    __tablename__ = "outbound_messages"
    __table_args__ = (
        CheckConstraint(sql_in("message_type", OutboundMessageType), name="message_type_check"),
        CheckConstraint(sql_in("status", OutboundStatus), name="status_check"),
        # delete/edit act on an existing bot message; a document is a report revision's file
        CheckConstraint(
            "message_type NOT IN ('delete', 'edit') OR target_wa_message_id IS NOT NULL",
            name="target_for_delete_edit",
        ),
        CheckConstraint(
            "message_type <> 'document' OR report_revision_id IS NOT NULL",
            name="revision_for_document",
        ),
        Index(
            "outbound_messages_retry_idx",
            "status",
            "created_at",
            postgresql_where=text("status IN ('pending', 'failed')"),
        ),
        Index("outbound_messages_wa_message_id_idx", "wa_message_id"),
        # FIFO per recipient
        Index(
            "outbound_messages_recipient_fifo_idx",
            "recipient_jid",
            "id",
            postgresql_where=text("status IN ('pending', 'sending')"),
        ),
    )

    id: Mapped[int] = _pk()
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    template_key: Mapped[str] = mapped_column(Text, nullable=False)
    message_type: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'text'"))
    recipient_jid: Mapped[str] = mapped_column(Text, nullable=False)
    quoted_wa_message_id: Mapped[str | None] = mapped_column(Text)
    target_wa_message_id: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    order_id: Mapped[int | None] = _fk("orders.id")
    report_revision_id: Mapped[int | None] = _fk("report_revisions.id")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    attempts: Mapped[int] = _int0()
    last_error: Mapped[str | None] = mapped_column(Text)
    wa_message_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = _ts_now()
    sent_at: Mapped[dt.datetime | None] = _ts()


class DailyReport(Base):
    __tablename__ = "daily_reports"
    __table_args__ = (CheckConstraint(sql_in("status", DailyReportStatus), name="status_check"),)

    id: Mapped[int] = _pk()
    report_date: Mapped[dt.date] = mapped_column(Date, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    due_at: Mapped[dt.datetime] = _ts(nullable=False)
    current_revision: Mapped[int] = _int0()
    pending_orders_count: Mapped[int] = _int0()
    last_checked_at: Mapped[dt.datetime | None] = _ts()
    pending_reminder_sent_at: Mapped[dt.datetime | None] = _ts()
    correction_requested_at: Mapped[dt.datetime | None] = _ts()
    reopened_by_override_id: Mapped[int | None] = _fk("schedule_overrides.id")
    created_at: Mapped[dt.datetime] = _ts_now()
    updated_at: Mapped[dt.datetime] = _ts_now(onupdate=True)


class ReportRevision(Base):
    __tablename__ = "report_revisions"
    __table_args__ = (
        UniqueConstraint("daily_report_id", "revision"),
        CheckConstraint(sql_in("kind", ReportRevisionKind), name="kind_check"),
        CheckConstraint(sql_in("status", ReportRevisionStatus), name="status_check"),
    )

    id: Mapped[int] = _pk()
    daily_report_id: Mapped[int] = _fk("daily_reports.id", nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot: Mapped[dict[str, Any]] = _jsonb(None)
    changes: Mapped[list[dict[str, Any]]] = _jsonb("[]")
    file_name: Mapped[str | None] = mapped_column(Text)
    file_content: Mapped[bytes | None] = mapped_column(LargeBinary)
    attempts: Mapped[int] = _int0()
    resends: Mapped[int] = _int0()
    last_error: Mapped[str | None] = mapped_column(Text)
    generated_at: Mapped[dt.datetime | None] = _ts()
    sent_at: Mapped[dt.datetime | None] = _ts()
    created_at: Mapped[dt.datetime] = _ts_now()


class ScheduleOverride(Base):
    __tablename__ = "schedule_overrides"
    __table_args__ = (
        CheckConstraint(sql_in("kind", ScheduleOverrideKind), name="kind_check"),
        CheckConstraint(sql_in("status", ScheduleOverrideStatus), name="status_check"),
        # Applied overrides must have a start; windows must have a valid end
        CheckConstraint(
            "kind = 'reopen'"
            " OR status IN ('pending_confirmation', 'rejected', 'expired', 'superseded')"
            " OR starts_at IS NOT NULL",
            name="period",
        ),
        CheckConstraint(
            "kind NOT IN ('closed_window', 'open_window')"
            " OR (starts_at IS NOT NULL AND ends_at IS NOT NULL AND starts_at < ends_at)",
            name="window",
        ),
        # 'applied' only for reopen; 'cancelled' only for closures; reopen is never 'active'
        CheckConstraint(
            "CASE kind"
            " WHEN 'reopen' THEN status NOT IN ('active', 'cancelled')"
            " WHEN 'open_window' THEN status NOT IN ('applied', 'cancelled')"
            " ELSE status <> 'applied'"
            " END",
            name="status_by_kind",
        ),
        # Affected business day: set for closed_today/closed_window/open_window, NULL otherwise
        CheckConstraint(
            "(business_date IS NOT NULL)"
            " = (kind IN ('closed_today', 'closed_window', 'open_window'))",
            name="business_date_by_kind",
        ),
        # At most one pending confirmation per admin (RN-24)
        Index(
            "schedule_overrides_one_pending_per_admin",
            "created_by",
            unique=True,
            postgresql_where=text("status = 'pending_confirmation'"),
        ),
        # Effective-hours lookup
        Index(
            "schedule_overrides_active",
            "starts_at",
            "ends_at",
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[int] = _pk()
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    business_date: Mapped[dt.date | None] = mapped_column(Date)
    starts_at: Mapped[dt.datetime | None] = _ts()
    ends_at: Mapped[dt.datetime | None] = _ts()
    reason: Mapped[str | None] = mapped_column(Text)
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    parsed: Mapped[dict[str, Any]] = _jsonb(None)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    inbound_event_id: Mapped[int | None] = _fk("inbound_events.id")
    confirmation_expires_at: Mapped[dt.datetime] = _ts(nullable=False)
    confirmation_wa_message_id: Mapped[str | None] = mapped_column(Text)
    confirmed_at: Mapped[dt.datetime | None] = _ts()
    cancelled_at: Mapped[dt.datetime | None] = _ts()
    cancelled_by_override_id: Mapped[int | None] = _fk("schedule_overrides.id")
    created_at: Mapped[dt.datetime] = _ts_now()


__all__ = [
    "Base",
    "Courier",
    "DailyReport",
    "InboundEvent",
    "Message",
    "Order",
    "OrderEvent",
    "OutboundMessage",
    "ReportRevision",
    "ScheduleOverride",
    "Store",
    "order_code_seq",
]
