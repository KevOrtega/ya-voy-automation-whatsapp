"""Per-sender limit on private messages (RF-05, CU-18 step 2): fixed one-minute Redis window."""

from __future__ import annotations

import time

from redis.asyncio import Redis

WINDOW_SECONDS = 60
KEY_PREFIX = "ratelimit:sender"


def window_key(sender_jid: str, now: float | None = None) -> str:
    bucket = int((time.time() if now is None else now) // WINDOW_SECONDS)
    return f"{KEY_PREFIX}:{sender_jid}:{bucket}"


async def hit(redis: Redis, sender_jid: str, limit: int, now: float | None = None) -> bool:
    """Count one message; ``True`` if the sender is still within ``limit`` for this minute."""
    key = window_key(sender_jid, now)
    pipe = redis.pipeline(transaction=True)
    pipe.incr(key)
    pipe.expire(key, WINDOW_SECONDS * 2)
    count, _ = await pipe.execute()
    return int(count) <= limit
