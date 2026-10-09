"""Redis lock primitives (token-safe release, NX contention, TTL)."""

from __future__ import annotations

import pytest
from arq.connections import ArqRedis

from ya_voy.locks import LockNotAcquiredError, redis_lock


async def test_acquire_sets_ttl_and_release_deletes(arq_redis: ArqRedis) -> None:
    async with redis_lock(arq_redis, "lock:test:a", ttl_seconds=30, wait_seconds=0) as lock:
        assert await lock.owned()
        assert 29_000 < await arq_redis.pttl("lock:test:a") <= 30_000
    assert await arq_redis.get("lock:test:a") is None


async def test_contention_waits_then_gives_up(arq_redis: ArqRedis) -> None:
    async with redis_lock(arq_redis, "lock:test:b", ttl_seconds=30, wait_seconds=0):
        with pytest.raises(LockNotAcquiredError):
            async with redis_lock(arq_redis, "lock:test:b", ttl_seconds=30, wait_seconds=0.2):
                pytest.fail("acquired a held lock")


async def test_release_only_by_owner(arq_redis: ArqRedis) -> None:
    async with redis_lock(arq_redis, "lock:test:c", ttl_seconds=30, wait_seconds=0) as lock:
        # Simulate expiry + another owner: our release must not delete their lock.
        await arq_redis.set("lock:test:c", "someone-else")
        assert not await lock.owned()
        assert not await lock.refresh()
    assert await arq_redis.get("lock:test:c") == b"someone-else"
    await arq_redis.delete("lock:test:c")


async def test_refresh_resets_ttl(arq_redis: ArqRedis) -> None:
    async with redis_lock(arq_redis, "lock:test:d", ttl_seconds=30, wait_seconds=0) as lock:
        await arq_redis.pexpire("lock:test:d", 1_000)
        assert await lock.refresh()
        assert await arq_redis.pttl("lock:test:d") > 29_000


async def test_released_on_error(arq_redis: ArqRedis) -> None:
    with pytest.raises(RuntimeError):
        async with redis_lock(arq_redis, "lock:test:e", ttl_seconds=30, wait_seconds=0):
            raise RuntimeError("boom")
    assert await arq_redis.get("lock:test:e") is None
