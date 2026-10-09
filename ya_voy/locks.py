"""Per-store Redis lock for order intake (docs/06 "Toma de pedidos").

The lock keeps one store's messages in order while the LLM runs **outside** any database
transaction. Its TTL is derived from the settings and is strictly greater than the worst-case
LLM time, so it cannot expire during a call; it is still released safely by token (only the owner
deletes it) and refreshed before every extraction. If the worker dies, the TTL frees the store.
"""

from __future__ import annotations

import asyncio
import math
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass

from redis.asyncio import Redis

from ya_voy.config import Settings
from ya_voy.llm.openai_compatible import OpenAICompatibleExtractor

KEY_PREFIX = "lock:store"
# Covers the short database phases around the LLM call and clock/network jitter.
MIN_SAFETY_MARGIN_SECONDS = 15.0
SAFETY_MARGIN_RATIO = 0.25
POLL_INITIAL_SECONDS = 0.05
POLL_MAX_SECONDS = 0.5

_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end
return 0
"""
_REFRESH = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('pexpire', KEYS[1], ARGV[2]) end
return 0
"""


class LockNotAcquiredError(Exception):
    """Another worker kept the store's lock for longer than the wait limit."""


def worst_case_llm_seconds(settings: Settings) -> float:
    """``LLM_TIMEOUT_SECONDS * (LLM_MAX_RETRIES + 1) + total max backoff``."""
    return OpenAICompatibleExtractor.worst_case_seconds(
        settings.llm_timeout_seconds, settings.llm_max_retries
    )


def store_lock_ttl_seconds(settings: Settings) -> float:
    """Worst-case LLM time plus ``max(15 s, 25 %)`` of it; always strictly greater."""
    worst = worst_case_llm_seconds(settings)
    return worst + max(MIN_SAFETY_MARGIN_SECONDS, worst * SAFETY_MARGIN_RATIO)


def store_lock_key(store_id: int) -> str:
    return f"{KEY_PREFIX}:{store_id}"


@dataclass
class RedisLock:
    redis: Redis
    key: str
    token: str
    ttl_ms: int

    async def refresh(self) -> bool:
        """Reset the TTL if we still own the lock (before each LLM call)."""
        return bool(await self.redis.eval(_REFRESH, 1, self.key, self.token, self.ttl_ms))

    async def owned(self) -> bool:
        value = await self.redis.get(self.key)
        if isinstance(value, bytes):
            value = value.decode()
        return value == self.token

    async def release(self) -> bool:
        return bool(await self.redis.eval(_RELEASE, 1, self.key, self.token))


@asynccontextmanager
async def redis_lock(
    redis: Redis, key: str, *, ttl_seconds: float, wait_seconds: float
) -> AsyncIterator[RedisLock]:
    """``SET key token NX PX ttl``, polling with backoff up to ``wait_seconds``."""
    lock = RedisLock(redis, key, secrets.token_hex(16), math.ceil(ttl_seconds * 1000))
    deadline = time.monotonic() + wait_seconds
    delay = POLL_INITIAL_SECONDS
    while not await redis.set(key, lock.token, nx=True, px=lock.ttl_ms):
        if time.monotonic() >= deadline:
            raise LockNotAcquiredError(key)
        await asyncio.sleep(delay)
        delay = min(delay * 2, POLL_MAX_SECONDS)
    try:
        yield lock
    finally:
        await lock.release()


def store_lock(
    redis: Redis, settings: Settings, store_id: int
) -> AbstractAsyncContextManager[RedisLock]:
    """The store's intake lock; waits at most one TTL (the holder's lease) to acquire it."""
    ttl = store_lock_ttl_seconds(settings)
    return redis_lock(redis, store_lock_key(store_id), ttl_seconds=ttl, wait_seconds=ttl)
