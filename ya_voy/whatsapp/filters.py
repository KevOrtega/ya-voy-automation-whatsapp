"""Discard what is obviously irrelevant before storing it (docs/06 §2 web, RF-02, CU-18 step 1)."""

from __future__ import annotations

from enum import StrEnum

from ya_voy.whatsapp import jids
from ya_voy.whatsapp.payloads import ConnectionUpdate, InboundMessage, OtherEvent, ParsedEvent


class DiscardReason(StrEnum):
    UNSUPPORTED_EVENT = "unsupported_event"
    OTHER_INSTANCE = "other_instance"
    FROM_ME = "from_me"
    STATUS_BROADCAST = "status_broadcast"
    OTHER_GROUP = "other_group"
    GROUP_NOT_REACTION = "group_not_reaction"
    NO_CHAT = "no_chat"


def discard_reason(
    event: ParsedEvent, *, instance: str, couriers_group_jid: str
) -> DiscardReason | None:
    """Why the webhook should drop ``event`` without storing it, or ``None`` to keep it.

    Whether a group reaction targets an order post needs the database; the worker checks it.
    """
    if isinstance(event, OtherEvent):
        return DiscardReason.UNSUPPORTED_EVENT
    if event.instance and event.instance != instance:
        return DiscardReason.OTHER_INSTANCE
    if isinstance(event, ConnectionUpdate):
        return None
    assert isinstance(event, InboundMessage)
    if event.from_me:
        return DiscardReason.FROM_ME
    if not event.remote_jid:
        return DiscardReason.NO_CHAT
    if jids.is_broadcast(event.remote_jid):
        return DiscardReason.STATUS_BROADCAST
    if jids.is_group(event.remote_jid):
        if event.remote_jid != couriers_group_jid:
            return DiscardReason.OTHER_GROUP
        if not event.is_reaction:
            return DiscardReason.GROUP_NOT_REACTION
    return None
