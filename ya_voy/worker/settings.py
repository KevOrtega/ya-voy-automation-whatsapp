"""arq ``WorkerSettings``."""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from arq import cron
from arq.connections import RedisSettings
from arq.cron import CronJob

from ya_voy.config import get_settings
from ya_voy.db.session import create_engine, create_session_factory
from ya_voy.llm.factory import LLMConfigError, build_order_extractor, build_schedule_parser
from ya_voy.llm.unavailable import UnavailableOrderExtractor, UnavailableScheduleParser
from ya_voy.logging import configure_logging
from ya_voy.queue import redis_settings_from_url
from ya_voy.whatsapp.client import EvolutionClient
from ya_voy.worker import jobs
from ya_voy.worker.drafts import evaluate_draft, expire_drafts
from ya_voy.worker.inbound import process_inbound_event
from ya_voy.worker.orders import alert_unclaimed_orders, retry_parked_reaction
from ya_voy.worker.outbound import send_outbound
from ya_voy.worker.reports import process_daily_reports, process_report_corrections
from ya_voy.worker.schedules import expire_schedule_confirmations, remind_still_closed
from ya_voy.worker.sweeps import recover_inbound_events, recover_outbound_messages

logger = logging.getLogger(__name__)


def cron_schedule(interval_seconds: int) -> dict[str, Any]:
    """arq ``cron`` kwargs that fire every ``interval_seconds`` (aligned to the clock).

    arq crons match on clock fields, so the interval is rounded to a divisor-friendly grid:
    under a minute it fires on the seconds ``0, interval, 2*interval, ...`` of each minute;
    from a minute up, on whole minutes.
    """
    if interval_seconds < 60:
        return {"second": set(range(0, 60, interval_seconds))}
    minutes = max(interval_seconds // 60, 1)
    return {"minute": set(range(0, 60, minutes)), "second": 0}


def build_cron_jobs(interval_seconds: int) -> list[CronJob]:
    schedule = cron_schedule(interval_seconds)
    # unique=True (default): one run at a time across workers; timeout well under the interval.
    return [
        cron(recover_inbound_events, run_at_startup=True, **schedule),
        cron(recover_outbound_messages, run_at_startup=True, **schedule),
        cron(expire_drafts, run_at_startup=True, **schedule),
        cron(alert_unclaimed_orders, run_at_startup=True, **schedule),
        cron(expire_schedule_confirmations, run_at_startup=True, **schedule),
        cron(remind_still_closed, run_at_startup=True, **schedule),
        cron(process_daily_reports, run_at_startup=True, **schedule),
        cron(process_report_corrections, run_at_startup=True, **schedule),
    ]


async def on_startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = create_engine(settings)
    ctx["settings"] = settings
    ctx["engine"] = engine
    ctx["session_factory"] = create_session_factory(engine)
    ctx["evolution"] = EvolutionClient.from_settings(settings)
    try:
        ctx["order_extractor"] = build_order_extractor(settings)
        ctx["schedule_parser"] = build_schedule_parser(settings)
    except LLMConfigError as exc:
        # Development only (production requires the LLM settings): calls then fail like an
        # outage (CU-01 6a, CU-14 1b).
        logger.error("LLM not configured", extra={"error": str(exc)})
        ctx["order_extractor"] = UnavailableOrderExtractor(str(exc))
        ctx["schedule_parser"] = UnavailableScheduleParser(str(exc))
    logger.info("worker started", extra={"app_env": settings.app_env})


async def on_shutdown(ctx: dict[str, Any]) -> None:
    for name in ("order_extractor", "schedule_parser"):
        client = ctx.get(name)
        if hasattr(client, "aclose"):
            await client.aclose()
    evolution = ctx.get("evolution")
    if evolution is not None:
        await evolution.aclose()
    engine = ctx.get("engine")
    if engine is not None:
        await engine.dispose()
    logger.info("worker stopped")


class WorkerSettings:
    functions: ClassVar[list[Any]] = [
        jobs.ping,
        process_inbound_event,
        send_outbound,
        evaluate_draft,
        retry_parked_reaction,
    ]
    # TODO(before production): retention cron apply_data_retention (RNF-10, DC-11);
    # see docs/plans/pending-before-production.md.
    cron_jobs: ClassVar[list[Any]] = build_cron_jobs(get_settings().scheduler_interval_seconds)
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings: RedisSettings = redis_settings_from_url(get_settings().redis_url)
