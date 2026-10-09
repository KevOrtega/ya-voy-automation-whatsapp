"""Shared arq/Redis helpers used by both the web app (enqueue) and the worker."""

from __future__ import annotations

from datetime import timedelta

from arq.connections import ArqRedis, RedisSettings

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
