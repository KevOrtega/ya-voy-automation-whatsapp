"""Minimal row builders for constraint tests (Core inserts, explicit values)."""

from __future__ import annotations

import datetime as dt
import itertools
from typing import Any

import pytest
from sqlalchemy import Insert, insert
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ya_voy.db import models as m

_seq = itertools.count(1)
TODAY = dt.date(2026, 10, 9)
NOW = dt.datetime(2026, 10, 9, 15, 0, tzinfo=dt.UTC)


def n() -> int:
    return next(_seq)


async def insert_row(conn: AsyncConnection, stmt: Insert) -> int:
    return (await conn.execute(stmt.returning(stmt.table.c.id))).scalar_one()


def store(**kw: Any) -> Insert:
    i = n()
    values = {"wa_jid": f"54911{i:08d}@s.whatsapp.net", "display_name": f"Store {i}"} | kw
    return insert(m.Store).values(**values)


def courier(**kw: Any) -> Insert:
    i = n()
    values = {"lid": f"{i:015d}@lid", "display_name": f"Courier {i}"} | kw
    return insert(m.Courier).values(**values)


def order(store_id: int, **kw: Any) -> Insert:
    values = {"store_id": store_id, "status": "draft", "business_date": TODAY} | kw
    return insert(m.Order).values(**values)


def published(store_id: int, **kw: Any) -> Insert:
    i = n()
    return order(
        store_id,
        **{"status": "published", "code": f"YV-T{i:04d}", "published_at": NOW, **kw},
    )


def inbound(**kw: Any) -> Insert:
    i = n()
    values = {
        "wa_message_id": f"WAMID{i}",
        "instance": "yavoy",
        "event_type": "messages.upsert",
        "payload": {"i": i},
    } | kw
    return insert(m.InboundEvent).values(**values)


def message(**kw: Any) -> Insert:
    values = {"direction": "in", "chat_jid": "x@s.whatsapp.net", "kind": "text"} | kw
    return insert(m.Message).values(**values)


def outbound(**kw: Any) -> Insert:
    i = n()
    values = {
        "idempotency_key": f"test:{i}",
        "template_key": "store_order_published",
        "recipient_jid": "x@s.whatsapp.net",
    } | kw
    return insert(m.OutboundMessage).values(**values)


def daily_report(**kw: Any) -> Insert:
    values = {"report_date": TODAY, "status": "waiting", "due_at": NOW} | kw
    return insert(m.DailyReport).values(**values)


def revision(daily_report_id: int, **kw: Any) -> Insert:
    values = {
        "daily_report_id": daily_report_id,
        "revision": 1,
        "kind": "initial",
        "status": "generating",
        "snapshot": {},
    } | kw
    return insert(m.ReportRevision).values(**values)


def override(**kw: Any) -> Insert:
    values = {
        "kind": "closed_today",
        "status": "pending_confirmation",
        "source_text": "hoy no tomamos pedidos por lluvia",
        "parsed": {"kind": "closed_today", "reason": "lluvia"},
        "created_by": "5491100000000",
        "confirmation_expires_at": NOW,
    } | kw
    return insert(m.ScheduleOverride).values(**values)


async def assert_rejected(engine: AsyncEngine, stmt: Any, constraint: str) -> None:
    """``stmt`` fails in its own transaction, naming ``constraint`` (or column, for NOT NULL)."""
    with pytest.raises((IntegrityError, DBAPIError)) as exc_info:
        async with engine.begin() as conn:
            await conn.execute(stmt)
    assert constraint in str(exc_info.value.orig), str(exc_info.value.orig)


async def insert_ok(engine: AsyncEngine, stmt: Insert) -> int:
    async with engine.begin() as conn:
        return await insert_row(conn, stmt)
