"""Allowed values of status/kind columns (docs/05-modelo-datos.md, docs/03-estados-pedido.md).

Single source for the database CHECK constraints and for the code.
"""

from __future__ import annotations

from enum import StrEnum


class OrderStatus(StrEnum):
    DRAFT = "draft"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    PUBLISHED = "published"
    ASSIGNED = "assigned"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


# Orders still "in preparation" (RN-09) and orders that hold back the daily report.
IN_PREPARATION_STATUSES = (OrderStatus.DRAFT, OrderStatus.AWAITING_CONFIRMATION)
PENDING_STATUSES = (*IN_PREPARATION_STATUSES, OrderStatus.PUBLISHED)


class PaymentMethod(StrEnum):
    CASH = "cash"
    TRANSFER = "transfer"
    PREPAID = "prepaid"
    OTHER = "other"


class CancelledBy(StrEnum):
    STORE = "store"
    ADMIN = "admin"


class ActorType(StrEnum):
    STORE = "store"
    COURIER = "courier"
    SYSTEM = "system"
    ADMIN = "admin"


class OrderEventType(StrEnum):
    """Documented values of order_events.event_type (not enforced by a CHECK in docs/05)."""

    CREATED = "created"
    DRAFT_UPDATED = "draft_updated"
    MISSING_FIELDS_REQUESTED = "missing_fields_requested"
    CONFIRMATION_REQUESTED = "confirmation_requested"
    CONFIRMED = "confirmed"
    PUBLISHED = "published"
    CLAIMED = "claimed"
    CLAIM_REJECTED = "claim_rejected"
    REACTION_ON_CANCELLED = "reaction_on_cancelled"
    RELEASED_BY_COURIER = "released_by_courier"
    RELEASED_BY_ADMIN = "released_by_admin"
    UNCLAIMED_ALERT_SENT = "unclaimed_alert_sent"
    CANCELLED = "cancelled"
    GROUP_POST_DELETED = "group_post_deleted"
    GROUP_POST_DELETE_FAILED = "group_post_delete_failed"
    EXPIRED = "expired"


class InboundEventStatus(StrEnum):
    RECEIVED = "received"
    PROCESSING = "processing"
    PROCESSED = "processed"
    IGNORED = "ignored"
    FAILED = "failed"


class MessageDirection(StrEnum):
    IN = "in"
    OUT = "out"


class MessageKind(StrEnum):
    """Documented values of messages.kind (not enforced by a CHECK in docs/05)."""

    TEXT = "text"
    REACTION = "reaction"
    IMAGE = "image"
    AUDIO = "audio"
    DOCUMENT = "document"
    OTHER = "other"


class OutboundMessageType(StrEnum):
    TEXT = "text"
    DOCUMENT = "document"
    DELETE = "delete"
    EDIT = "edit"


class OutboundStatus(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"


class DailyReportStatus(StrEnum):
    WAITING = "waiting"
    GENERATING = "generating"
    SENT = "sent"
    CORRECTION_PENDING = "correction_pending"
    FAILED = "failed"


class ReportRevisionKind(StrEnum):
    INITIAL = "initial"
    CORRECTION = "correction"


class ReportRevisionStatus(StrEnum):
    GENERATING = "generating"
    GENERATED = "generated"
    SENT = "sent"
    FAILED = "failed"


class ScheduleOverrideKind(StrEnum):
    CLOSED_TODAY = "closed_today"
    CLOSED_UNTIL_FURTHER_NOTICE = "closed_until_further_notice"
    CLOSED_WINDOW = "closed_window"
    OPEN_WINDOW = "open_window"
    REOPEN = "reopen"


class ScheduleOverrideStatus(StrEnum):
    PENDING_CONFIRMATION = "pending_confirmation"
    ACTIVE = "active"
    APPLIED = "applied"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
