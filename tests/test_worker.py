from __future__ import annotations

from ya_voy.queue import redis_settings_from_url
from ya_voy.worker import WorkerSettings
from ya_voy.worker.jobs import ping


def test_redis_settings_from_url() -> None:
    rs = redis_settings_from_url("redis://:pw@redis-host:6380/2")
    assert (rs.host, rs.port, rs.database, rs.password) == ("redis-host", 6380, 2, "pw")


def test_worker_settings_registers_jobs() -> None:
    assert ping in WorkerSettings.functions
    assert WorkerSettings.redis_settings is not None


async def test_ping_job() -> None:
    assert await ping({"job_id": "1"}) == "pong"
    assert await ping({}, "hi") == "hi"
