"""Where a stored inbound event goes next. Stubs until the business stages land.

Pure function so it is trivial to test; the handlers for each route come in stage 4+ (orders,
identity), stage 5 (group reactions) and stage 7 (admin alerts).
"""

from __future__ import annotations

from enum import StrEnum

from ya_voy.whatsapp import jids
from ya_voy.whatsapp.payloads import ConnectionUpdate, InboundMessage, ParsedEvent


class Route(StrEnum):
    PRIVATE_TEXT = "private_text"
    PRIVATE_NON_TEXT = "private_non_text"
    GROUP_REACTION = "group_reaction"
    GROUP_REACTION_REMOVAL = "group_reaction_removal"
    CONNECTION_UPDATE = "connection_update"
    UNHANDLED = "unhandled"


# Routes that do nothing yet; the event ends as ``processed``. UNHANDLED ends as ``ignored``.
def route_event(event: ParsedEvent, *, couriers_group_jid: str) -> Route:
    if isinstance(event, ConnectionUpdate):
        # TODO(stage 7): admin_whatsapp_disconnected on state "close", sent on reconnection.
        return Route.CONNECTION_UPDATE
    if not isinstance(event, InboundMessage) or event.from_me:
        return Route.UNHANDLED
    if jids.is_group(event.remote_jid):
        if event.remote_jid != couriers_group_jid or not event.is_reaction:
            return Route.UNHANDLED
        # TODO(stage 5): claim / release on the order post (reaction_target_id), @lid identity.
        return Route.GROUP_REACTION_REMOVAL if event.is_reaction_removal else Route.GROUP_REACTION
    if jids.is_broadcast(event.remote_jid) or not event.remote_jid:
        return Route.UNHANDLED
    # TODO(stage 4): classify the sender (admin / courier / store), auto-register stores,
    # drafts and LLM extraction; non-text messages get the "text only" reply.
    return Route.PRIVATE_TEXT if event.text else Route.PRIVATE_NON_TEXT


def is_private(route: Route) -> bool:
    return route in (Route.PRIVATE_TEXT, Route.PRIVATE_NON_TEXT)
