"""Draft jobs: ``evaluate_draft`` (RF-15..RF-17) and the ``expire_drafts`` cron (T09, CU-12)."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ya_voy.config import Settings
from ya_voy.llm.extractor import OrderExtractor
from ya_voy.queue import enqueue_evaluate_draft, enqueue_outbound_ids
from ya_voy.worker import intake

logger = logging.getLogger(__name__)


async def evaluate_draft(ctx: dict[str, Any], order_id: int) -> str:
    settings: Settings = ctx["settings"]
    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    async with session_factory() as session, session.begin():
        result = await intake.evaluate_draft(session, settings, order_id)
    await enqueue_outbound_ids(ctx["redis"], result.outbound_ids)
    logger.info("draft evaluated", extra={"order_id": order_id, "outcome": result.outcome})
    return result.outcome


async def expire_drafts(ctx: dict[str, Any]) -> int:
    settings: Settings = ctx["settings"]
    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]
    async with session_factory() as session, session.begin():
        ids = await intake.expire_drafts(session, settings)
    await enqueue_outbound_ids(ctx["redis"], ids)
    if ids:
        logger.info("drafts expired", extra={"count": len(ids)})
    return len(ids)


async def handle_store_text(
    ctx: dict[str, Any], session: AsyncSession, msg: intake.StoreText
) -> intake.IntakeResult:
    """Entry point for stage 4b: run inside the inbound event's transaction.

    The caller commits, then calls :func:`after_commit` with the result.
    """
    extractor: OrderExtractor = ctx["order_extractor"]
    return await intake.handle_store_text(session, ctx["settings"], extractor, msg)


async def after_commit(ctx: dict[str, Any], result: intake.IntakeResult) -> None:
    settings: Settings = ctx["settings"]
    await enqueue_outbound_ids(ctx["redis"], result.outbound_ids)
    if result.evaluate and result.order_id is not None:
        await enqueue_evaluate_draft(
            ctx["redis"],
            result.order_id,
            defer_by=timedelta(seconds=settings.draft_debounce_seconds),
        )
