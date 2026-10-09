"""Shared arq/Redis helpers used by both the web app (enqueue) and the worker."""

from arq.connections import RedisSettings


def redis_settings_from_url(url: str) -> RedisSettings:
    """Build arq ``RedisSettings`` from ``REDIS_URL`` (``redis://`` or ``rediss://``)."""
    return RedisSettings.from_dsn(url)
