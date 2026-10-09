"""Every constraint in docs/05-modelo-datos.md rejects invalid data on the real database."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.integration import factories as f
from ya_voy.db import models as m

pytestmark = pytest.mark.asyncio


# --- stores -------------------------------------------------------------------------------


async def test_store_defaults(engine: AsyncEngine) -> None:
    sid = await f.insert_ok(engine, f.store())
    async with engine.connect() as conn:
        row = (await conn.execute(select(m.Store).where(m.Store.id == sid))).one()
    assert row.is_blocked is False
    assert row.created_at is not None and row.updated_at is not None
    assert row.phone is None and row.lid is None


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("wa_jid", "5491111111111@s.whatsapp.net", "stores_wa_jid_key"),
        ("phone", "5491111111111", "stores_phone_key"),
        ("lid", "123456789@lid", "stores_lid_key"),
    ],
)
async def test_store_identifiers_unique(
    engine: AsyncEngine, column: str, value: str, constraint: str
) -> None:
    await f.insert_ok(engine, f.store(**{column: value}))
    await f.assert_rejected(engine, f.store(**{column: value}), constraint)


async def test_store_nullable_identifiers_allow_many_nulls(engine: AsyncEngine) -> None:
    await f.insert_ok(engine, f.store(phone=None, lid=None))
    await f.insert_ok(engine, f.store(phone=None, lid=None))


@pytest.mark.parametrize("column", ["wa_jid", "display_name"])
async def test_store_required_columns(engine: AsyncEngine, column: str) -> None:
    await f.assert_rejected(engine, f.store(**{column: None}), f'column "{column}"')


# --- couriers -----------------------------------------------------------------------------


async def test_courier_needs_lid_or_phone_jid(engine: AsyncEngine) -> None:
    await f.assert_rejected(
        engine, f.courier(lid=None, phone_jid=None, phone="5491100000001"), "couriers_has_identity"
    )
    await f.insert_ok(engine, f.courier(lid=None, phone_jid="5491100000002@s.whatsapp.net"))
    await f.insert_ok(engine, f.courier(lid="999@lid", phone_jid=None))


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("lid", "1234@lid", "couriers_lid_key"),
        ("phone_jid", "5491100000003@s.whatsapp.net", "couriers_phone_jid_key"),
        ("phone", "5491100000003", "couriers_phone_key"),
    ],
)
async def test_courier_identifiers_unique(
    engine: AsyncEngine, column: str, value: str, constraint: str
) -> None:
    await f.insert_ok(engine, f.courier(**{column: value}))
    await f.assert_rejected(engine, f.courier(**{column: value}), constraint)


# --- orders -------------------------------------------------------------------------------


async def test_order_defaults(engine: AsyncEngine) -> None:
    sid = await f.insert_ok(engine, f.store())
    oid = await f.insert_ok(engine, f.order(sid))
    async with engine.connect() as conn:
        row = (await conn.execute(select(m.Order).where(m.Order.id == oid))).one()
    assert row.extraction == {}
    assert row.validation_errors == []
    assert row.missing_prompts_count == 0
    assert row.code is None and row.courier_id is None


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("status", "delivered", "orders_status_check"),
        ("payment_method", "bitcoin", "orders_payment_method_check"),
        ("cancelled_by", "courier", "orders_cancelled_by_check"),
    ],
)
async def test_order_enumerated_columns(
    engine: AsyncEngine, column: str, value: str, constraint: str
) -> None:
    sid = await f.insert_ok(engine, f.store())
    await f.assert_rejected(engine, f.order(sid, **{column: value}), constraint)


@pytest.mark.parametrize("column", ["store_id", "status", "business_date"])
async def test_order_required_columns(engine: AsyncEngine, column: str) -> None:
    sid = await f.insert_ok(engine, f.store())
    stmt = f.order(sid).values(**{column: None})
    await f.assert_rejected(engine, stmt, f'column "{column}"')


async def test_order_store_fk(engine: AsyncEngine) -> None:
    await f.assert_rejected(engine, f.order(424242), "orders_store_id_fkey")


async def test_order_courier_fks(engine: AsyncEngine) -> None:
    sid = await f.insert_ok(engine, f.store())
    await f.assert_rejected(
        engine, f.published(sid, last_courier_id=424242), "orders_last_courier_id_fkey"
    )
    await f.assert_rejected(
        engine, f.published(sid, status="assigned", courier_id=424242), "orders_courier_id_fkey"
    )


@pytest.mark.parametrize("status", ["draft", "awaiting_confirmation"])
async def test_one_order_in_preparation_per_store_rn09(engine: AsyncEngine, status: str) -> None:
    sid = await f.insert_ok(engine, f.store())
    await f.insert_ok(engine, f.order(sid, status="draft"))
    await f.assert_rejected(engine, f.order(sid, status=status), "orders_one_draft_per_store")


async def test_draft_allowed_again_once_previous_left_preparation(engine: AsyncEngine) -> None:
    sid = await f.insert_ok(engine, f.store())
    other = await f.insert_ok(engine, f.store())
    first = await f.insert_ok(engine, f.order(sid, status="awaiting_confirmation"))
    await f.insert_ok(engine, f.order(other))  # a different store is independent
    async with engine.begin() as conn:
        await conn.execute(
            update(m.Order)
            .where(m.Order.id == first)
            .values(status="published", code="YV-0001", published_at=f.NOW)
        )
    await f.insert_ok(engine, f.order(sid))  # new draft while the first one is published
    for status in ("published", "cancelled", "expired"):  # any number outside preparation
        extra = {"code": f"YV-X{f.n()}", "published_at": f.NOW} if status == "published" else {}
        await f.insert_ok(engine, f.order(sid, status=status, **extra))


async def test_courier_matches_status(engine: AsyncEngine) -> None:
    sid = await f.insert_ok(engine, f.store())
    cid = await f.insert_ok(engine, f.courier())
    # assigned without courier
    await f.assert_rejected(
        engine, f.published(sid, status="assigned"), "orders_courier_matches_status"
    )
    # courier on any other status
    for status in ("draft", "awaiting_confirmation", "published", "cancelled", "expired"):
        stmt = f.published(sid, status=status, courier_id=cid)
        await f.assert_rejected(engine, stmt, "orders_courier_matches_status")
    await f.insert_ok(engine, f.published(sid, status="assigned", courier_id=cid))


async def test_cancelling_assigned_order_must_clear_courier(engine: AsyncEngine) -> None:
    """T17 sets courier_id = NULL; leaving it set is rejected by the invariant."""
    sid = await f.insert_ok(engine, f.store())
    cid = await f.insert_ok(engine, f.courier())
    oid = await f.insert_ok(
        engine, f.published(sid, status="assigned", courier_id=cid, last_courier_id=cid)
    )
    await f.assert_rejected(
        engine,
        update(m.Order).where(m.Order.id == oid).values(status="cancelled"),
        "orders_courier_matches_status",
    )
    async with engine.begin() as conn:
        await conn.execute(
            update(m.Order)
            .where(m.Order.id == oid)
            .values(status="cancelled", courier_id=None, cancelled_by="store")
        )


async def test_code_when_published(engine: AsyncEngine) -> None:
    sid = await f.insert_ok(engine, f.store())
    await f.assert_rejected(
        engine, f.order(sid, status="published", published_at=f.NOW), "orders_code_when_published"
    )


@pytest.mark.parametrize(
    ("column", "constraint"),
    [("code", "orders_code_key"), ("group_message_id", "orders_group_message_id_key")],
)
async def test_order_unique_columns(engine: AsyncEngine, column: str, constraint: str) -> None:
    sid = await f.insert_ok(engine, f.store())
    await f.insert_ok(engine, f.published(sid, code="YV-0001", group_message_id="GRP1"))
    dup = {"code": "YV-0001"} if column == "code" else {"group_message_id": "GRP1"}
    await f.assert_rejected(engine, f.published(sid, **dup), constraint)


# --- order_events -------------------------------------------------------------------------


async def test_order_event_constraints(engine: AsyncEngine) -> None:
    sid = await f.insert_ok(engine, f.store())
    oid = await f.insert_ok(engine, f.order(sid))
    base = {"order_id": oid, "event_type": "created", "actor_type": "store"}
    eid = await f.insert_ok(engine, f.insert(m.OrderEvent).values(**base))
    async with engine.connect() as conn:
        data = await conn.scalar(select(m.OrderEvent.data).where(m.OrderEvent.id == eid))
    assert data == {}
    await f.assert_rejected(
        engine,
        f.insert(m.OrderEvent).values(**{**base, "actor_type": "robot"}),
        "order_events_actor_type_check",
    )
    await f.assert_rejected(
        engine,
        f.insert(m.OrderEvent).values(**{**base, "order_id": 424242}),
        "order_events_order_id_fkey",
    )
    for fk, constraint in (
        ("courier_id", "order_events_courier_id_fkey"),
        ("inbound_event_id", "order_events_inbound_event_id_fkey"),
    ):
        await f.assert_rejected(
            engine, f.insert(m.OrderEvent).values(**{**base, fk: 424242}), constraint
        )


# --- inbound_events -----------------------------------------------------------------------


async def test_inbound_event_defaults_and_checks(engine: AsyncEngine) -> None:
    iid = await f.insert_ok(engine, f.inbound())
    async with engine.connect() as conn:
        row = (await conn.execute(select(m.InboundEvent).where(m.InboundEvent.id == iid))).one()
    assert row.status == "received" and row.attempts == 0 and row.received_at is not None
    await f.assert_rejected(engine, f.inbound(status="done"), "inbound_events_status_check")
    # Python None must reach the database as SQL NULL (not JSON null) and be rejected.
    await f.assert_rejected(engine, f.inbound(payload=None), 'column "payload"')


async def test_inbound_event_wa_message_id_unique_rf59(engine: AsyncEngine) -> None:
    await f.insert_ok(engine, f.inbound(wa_message_id="ABC"))
    await f.assert_rejected(
        engine, f.inbound(wa_message_id="ABC"), "inbound_events_wa_message_id_key"
    )


# --- messages -----------------------------------------------------------------------------


async def test_message_constraints(engine: AsyncEngine) -> None:
    await f.insert_ok(engine, f.message())
    await f.assert_rejected(engine, f.message(direction="both"), "messages_direction_check")
    await f.assert_rejected(engine, f.message(kind=None), 'column "kind"')
    for fk in ("store_id", "courier_id", "order_id", "inbound_event_id", "outbound_message_id"):
        await f.assert_rejected(engine, f.message(**{fk: 424242}), f"messages_{fk}_fkey")


# --- outbound_messages --------------------------------------------------------------------


async def test_outbound_defaults_and_checks(engine: AsyncEngine) -> None:
    oid = await f.insert_ok(engine, f.outbound())
    async with engine.connect() as conn:
        row = (
            await conn.execute(select(m.OutboundMessage).where(m.OutboundMessage.id == oid))
        ).one()
    assert (row.message_type, row.status, row.body, row.attempts) == ("text", "pending", "", 0)
    await f.assert_rejected(
        engine, f.outbound(message_type="audio"), "outbound_messages_message_type_check"
    )
    await f.assert_rejected(engine, f.outbound(status="queued"), "outbound_messages_status_check")
    await f.assert_rejected(engine, f.outbound(idempotency_key="k1", body=None), 'column "body"')
    for fk in ("order_id", "report_revision_id"):
        await f.assert_rejected(engine, f.outbound(**{fk: 424242}), f"outbound_messages_{fk}_fkey")


async def test_outbound_idempotency_key_unique_rf60(engine: AsyncEngine) -> None:
    await f.insert_ok(engine, f.outbound(idempotency_key="order:42:claimed:courier:7"))
    await f.assert_rejected(
        engine,
        f.outbound(idempotency_key="order:42:claimed:courier:7"),
        "outbound_messages_idempotency_key_key",
    )


# --- daily_reports / report_revisions ------------------------------------------------------


async def test_daily_report_constraints(engine: AsyncEngine) -> None:
    rid = await f.insert_ok(engine, f.daily_report())
    async with engine.connect() as conn:
        row = (await conn.execute(select(m.DailyReport).where(m.DailyReport.id == rid))).one()
    assert (row.current_revision, row.pending_orders_count) == (0, 0)
    await f.assert_rejected(engine, f.daily_report(), "daily_reports_report_date_key")
    other_day = f.TODAY + dt.timedelta(days=1)
    await f.assert_rejected(
        engine, f.daily_report(report_date=other_day, status="done"), "daily_reports_status_check"
    )
    await f.assert_rejected(
        engine, f.daily_report(report_date=other_day, due_at=None), 'column "due_at"'
    )
    await f.assert_rejected(
        engine,
        f.daily_report(report_date=other_day, reopened_by_override_id=424242),
        "daily_reports_reopened_by_override_id_fkey",
    )


async def test_report_revision_constraints(engine: AsyncEngine) -> None:
    rid = await f.insert_ok(engine, f.daily_report())
    vid = await f.insert_ok(engine, f.revision(rid))
    async with engine.connect() as conn:
        changes = await conn.scalar(
            select(m.ReportRevision.changes).where(m.ReportRevision.id == vid)
        )
    assert changes == []
    await f.assert_rejected(
        engine, f.revision(rid), "report_revisions_daily_report_id_revision_key"
    )
    await f.insert_ok(engine, f.revision(rid, revision=2, kind="correction"))
    await f.assert_rejected(
        engine, f.revision(rid, revision=3, kind="patch"), "report_revisions_kind_check"
    )
    await f.assert_rejected(
        engine, f.revision(rid, revision=3, status="queued"), "report_revisions_status_check"
    )
    await f.assert_rejected(engine, f.revision(rid, revision=3, snapshot=None), 'column "snapshot"')
    await f.assert_rejected(engine, f.revision(424242), "report_revisions_daily_report_id_fkey")


# --- schedule_overrides -------------------------------------------------------------------


async def test_override_kind_and_status_checks(engine: AsyncEngine) -> None:
    await f.assert_rejected(engine, f.override(kind="closed_forever"), "kind_check")
    await f.assert_rejected(
        engine, f.override(status="done", starts_at=f.NOW), "schedule_overrides_status_check"
    )
    await f.assert_rejected(
        engine, f.override(confirmation_expires_at=None), 'column "confirmation_expires_at"'
    )


async def test_one_pending_confirmation_per_admin_rn24(engine: AsyncEngine) -> None:
    first = await f.insert_ok(engine, f.override())
    await f.assert_rejected(engine, f.override(), "schedule_overrides_one_pending_per_admin")
    await f.insert_ok(engine, f.override(created_by="5491199999999"))  # another admin
    async with engine.begin() as conn:  # the new instruction supersedes the old one
        await conn.execute(
            update(m.ScheduleOverride)
            .where(m.ScheduleOverride.id == first)
            .values(status="superseded")
        )
    await f.insert_ok(engine, f.override())


async def test_override_period_requires_start_once_applied(engine: AsyncEngine) -> None:
    for status in ("active", "cancelled", "applied"):
        await f.assert_rejected(
            engine, f.override(status=status, starts_at=None), "schedule_overrides_period"
        )
    for i, status in enumerate(("rejected", "expired", "superseded")):
        await f.insert_ok(engine, f.override(status=status, created_by=f"54911000000{i}"))
    await f.insert_ok(engine, f.override(kind="reopen", status="applied"))
    await f.insert_ok(engine, f.override(status="active", starts_at=f.NOW))


@pytest.mark.parametrize("kind", ["closed_window", "open_window"])
async def test_override_window_needs_valid_end(engine: AsyncEngine, kind: str) -> None:
    start = f.NOW
    for ends_at in (None, start, start - dt.timedelta(hours=1)):
        await f.assert_rejected(
            engine,
            f.override(kind=kind, starts_at=start, ends_at=ends_at),
            "schedule_overrides_window",
        )
    await f.insert_ok(
        engine, f.override(kind=kind, starts_at=start, ends_at=start + dt.timedelta(hours=3))
    )


async def test_override_self_reference_fk(engine: AsyncEngine) -> None:
    await f.assert_rejected(
        engine,
        f.override(status="rejected", cancelled_by_override_id=424242),
        "schedule_overrides_cancelled_by_override_id_fkey",
    )
    reopen = await f.insert_ok(engine, f.override(kind="reopen", status="applied"))
    await f.insert_ok(
        engine,
        f.override(
            status="cancelled",
            starts_at=f.NOW,
            cancelled_at=f.NOW,
            cancelled_by_override_id=reopen,
            created_by="5491122222222",
        ),
    )


# --- identity -----------------------------------------------------------------------------


async def test_ids_are_generated_always(engine: AsyncEngine) -> None:
    await f.assert_rejected(
        engine,
        text("INSERT INTO stores (id, wa_jid, display_name) VALUES (1, 'a@s.whatsapp.net', 'a')"),
        'cannot insert a non-DEFAULT value into column "id"',
    )
