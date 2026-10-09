"""arq ``WorkerSettings``."""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from arq.connections import RedisSettings

from ya_voy.config import get_settings
from ya_voy.db.session import create_engine, create_session_factory
from ya_voy.logging import configure_logging
from ya_voy.queue import redis_settings_from_url
from ya_voy.worker import jobs

logger = logging.getLogger(__name__)


async def on_startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = create_engine(settings)
    ctx["settings"] = settings
    ctx["engine"] = engine
    ctx["session_factory"] = create_session_factory(engine)
    logger.info("worker started", extra={"app_env": settings.app_env})


async def on_shutdown(ctx: dict[str, Any]) -> None:
    engine = ctx.get("engine")
    if engine is not None:
        await engine.dispose()
    logger.info("worker stopped")


class WorkerSettings:
    functions: ClassVar[list[Any]] = [jobs.ping]
    # TODO(stage 4+): cron_jobs for the scheduled tasks, every SCHEDULER_INTERVAL_SECONDS.
    cron_jobs: ClassVar[list[Any]] = []
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings: RedisSettings = redis_settings_from_url(get_settings().redis_url)
