"""Normalized inputs of the order core (no Evolution payloads here).

Stage 5b builds them from ``messages.upsert`` reactions: it resolves ``reactionMessage.key.id``
to ``group_message_id`` and the participant (``@lid`` or phone JID, with ``pushName``) to a
``couriers`` row, creating it if needed (RF-22, CU-04 step 3).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReactionAdded:
    """A non-empty reaction on a bot message in the couriers group (CU-04, CU-07)."""

    group_message_id: str
    courier_id: int
    emoji: str
    inbound_event_id: int | None = None


@dataclass(frozen=True)
class ReactionRemoved:
    """An empty reaction (the participant removed theirs) on a bot message (CU-06)."""

    group_message_id: str
    courier_id: int
    inbound_event_id: int | None = None
