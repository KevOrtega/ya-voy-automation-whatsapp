"""Schedule jobs: admin entry point for stage 7b and the crons ``expire_schedule_confirmations``
and ``remind_still_closed`` (docs/06 "Tareas programadas")."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ya_voy.llm.extractor import ScheduleIntentParser
from ya_voy.queue import enqueue_outbound_ids
from ya_voy.worker import schedule_admin
from ya_voy.worker.schedule_admin import AdminResult, AdminText, Finalize

logger = logging.getLogger(__name__)


async def handle_admin_text(
    ctx: dict[str, Any], msg: AdminText, *, finalize: Finalize | None = None
) -> AdminResult:
    """Entry point for stage 7b. Call it outside any open transaction (it commits on its own),
    then :func:`after_commit` with the result."""
    parser: ScheduleIntentParser = ctx["schedule_parser"]
    return await schedule_admin.handle_admin_text(
        ctx["session_factory"], ctx["redis"], ctx["settings"], parser, msg, finalize=finalize
    )


async def after_commit(ctx: dict[str, Any], result: AdminResult) -> None:
    await enqueue_outbound_ids(ctx["redis"], result.outbound_ids)


async def expire_schedule_confirmations(ctx: dict[str, Any]) -> int:
    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    async with session_factory() as session, session.begin():
        ids = await schedule_admin.expire_schedule_confirmations(session)
    if ids:
        logger.info("schedule confirmations expired", extra={"count": len(ids)})
    return len(ids)


async def remind_still_closed(ctx: dict[str, Any]) -> int:
    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    async with session_factory() as session, session.begin():
        ids = await schedule_admin.remind_still_closed(session, ctx["settings"])
    await enqueue_outbound_ids(ctx["redis"], ids)
    return len(ids)
