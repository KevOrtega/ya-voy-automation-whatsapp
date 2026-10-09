"""Report crons ``process_daily_reports`` and ``process_report_corrections`` (docs/06
"Tareas programadas", CU-16, CU-17). Both drive the same per-day step machine in
``ya_voy.reports.service``; the split only decides which days each one looks at."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ya_voy.config import Settings
from ya_voy.queue import enqueue_outbound_ids
from ya_voy.reports import service
from ya_voy.schedule import repository as schedule_repository

logger = logging.getLogger(__name__)


async def _run(ctx: dict[str, Any], days: list[dt.date]) -> dict[str, str]:
    settings: Settings = ctx["settings"]
    outcomes: dict[str, str] = {}
    for day in days:
        try:
            outcome, ids = await service.run_step(ctx["session_factory"], settings, day)
        except Exception:
            logger.exception("daily report step failed", extra={"report_date": str(day)})
            outcomes[str(day)] = "error"
            continue
        await enqueue_outbound_ids(ctx["redis"], ids)
        outcomes[str(day)] = str(outcome)
    return outcomes


async def process_daily_reports(ctx: dict[str, Any]) -> dict[str, str]:
    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    async with session_factory() as session, session.begin():
        now = await schedule_repository.db_now(session)
        days = await service.report_dates(session, ctx["settings"], now)
    return await _run(ctx, days)


async def process_report_corrections(ctx: dict[str, Any]) -> dict[str, str]:
    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    async with session_factory() as session, session.begin():
        now = await schedule_repository.db_now(session)
        days = await service.correction_dates(session, now)
    return await _run(ctx, days)
