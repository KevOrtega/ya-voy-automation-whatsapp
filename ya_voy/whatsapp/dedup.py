"""The ``inbound_events.wa_message_id`` dedup key (RF-59, CU-11). Single place to change it.

docs/05: ``data.key.id`` of the event; a hash of the payload for events without a message id.

TODO(verify-evolution): if removing a reaction reuses the original reaction's ``key.id``, the
removal would be dropped as a duplicate. Then the key for reactions becomes a hash of
``key.id`` + reaction text + ``messageTimestamp`` (docs/05, inbound_events). Only this function
changes; the webhook and the tests import it from here.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ya_voy.whatsapp.payloads import InboundMessage, ParsedEvent

PAYLOAD_HASH_PREFIX = "sha256:"


def payload_hash(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return PAYLOAD_HASH_PREFIX + hashlib.sha256(canonical.encode()).hexdigest()


def compute_inbound_dedup_key(event: ParsedEvent, payload: Any) -> str:
    """Key stored in ``inbound_events.wa_message_id`` (unique)."""
    if isinstance(event, InboundMessage) and event.wa_message_id:
        return event.wa_message_id
    return payload_hash(payload)
