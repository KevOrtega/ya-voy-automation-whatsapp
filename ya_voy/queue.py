"""Shared arq/Redis helpers used by both the web app (enqueue) and the worker."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import timedelta

from arq.connections import ArqRedis, RedisSettings

logger = logging.getLogger(__name__)

PROCESS_INBOUND_EVENT = "process_inbound_event"
SEND_OUTBOUND = "send_outbound"


def redis_settings_from_url(url: str) -> RedisSettings:
    """Build arq ``RedisSettings`` from ``REDIS_URL`` (``redis://`` or ``rediss://``)."""
    return RedisSettings.from_dsn(url)


async def enqueue_process_inbound_event(redis: ArqRedis, inbound_event_id: int) -> None:
    # No arq job id: duplicates are harmless (the job is idempotent) and a fixed id would block
    # re-enqueueing by the recovery sweep while arq keeps the previous result.
    await redis.enqueue_job(PROCESS_INBOUND_EVENT, inbound_event_id)


async def enqueue_send_outbound(
    redis: ArqRedis, outbound_message_id: int, *, defer_by: timedelta | None = None
) -> None:
    await redis.enqueue_job(SEND_OUTBOUND, outbound_message_id, _defer_by=defer_by)


async def enqueue_outbound_ids(redis: ArqRedis, outbound_message_ids: Iterable[int]) -> None:
    """Enqueue ``send_outbound`` after a commit; failures are left to the recovery sweep."""
    for outbound_id in outbound_message_ids:
        try:
            await enqueue_send_outbound(redis, outbound_id)
        except Exception:
            logger.exception(
                "could not enqueue outbound message", extra={"outbound_message_id": outbound_id}
            )
