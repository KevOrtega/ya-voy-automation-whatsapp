"""Pure parts of the worker: routing, cron schedule, backoff, rate-limit window, catalog."""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.evolution_fixtures import COURIERS_GROUP_JID, load_fixture
from ya_voy.messaging import catalog
from ya_voy.whatsapp.payloads import parse_webhook
from ya_voy.worker import WorkerSettings
from ya_voy.worker.drafts import evaluate_draft, expire_drafts
from ya_voy.worker.inbound import process_inbound_event
from ya_voy.worker.orders import alert_unclaimed_orders
from ya_voy.worker.outbound import backoff_delay, send_outbound
from ya_voy.worker.ratelimit import window_key
from ya_voy.worker.reports import process_daily_reports, process_report_corrections
from ya_voy.worker.routing import Route, is_private, route_event
from ya_voy.worker.schedules import expire_schedule_confirmations, remind_still_closed
from ya_voy.worker.settings import build_cron_jobs, cron_schedule
from ya_voy.worker.sweeps import recover_inbound_events, recover_outbound_messages


@pytest.mark.parametrize(
    ("name", "route"),
    [
        ("private_text", Route.PRIVATE_TEXT),
        ("private_text_lid_with_alt", Route.PRIVATE_TEXT),
        ("private_text_lid_only", Route.PRIVATE_TEXT),
        ("private_quoted_cancel", Route.PRIVATE_TEXT),
        ("private_image", Route.PRIVATE_NON_TEXT),
        ("group_reaction", Route.GROUP_REACTION),
        ("group_reaction_lid_only", Route.GROUP_REACTION),
        ("group_reaction_removal", Route.GROUP_REACTION_REMOVAL),
        ("connection_update_close", Route.CONNECTION_UPDATE),
        # Normally dropped by the webhook; the worker stays safe if one is stored anyway.
        ("group_text", Route.UNHANDLED),
        ("other_group_reaction", Route.UNHANDLED),
        ("private_from_me", Route.UNHANDLED),
        ("status_broadcast", Route.UNHANDLED),
        ("unsupported_event", Route.UNHANDLED),
    ],
)
def test_route_event(name: str, route: Route) -> None:
    event = parse_webhook(load_fixture(name))
    assert route_event(event, couriers_group_jid=COURIERS_GROUP_JID) is route


def test_only_private_routes_are_rate_limited() -> None:
    assert {r for r in Route if is_private(r)} == {Route.PRIVATE_TEXT, Route.PRIVATE_NON_TEXT}


def test_worker_registers_stage_3_jobs_and_sweeps() -> None:
    assert process_inbound_event in WorkerSettings.functions
    assert send_outbound in WorkerSettings.functions
    assert evaluate_draft in WorkerSettings.functions
    names = {job.coroutine for job in WorkerSettings.cron_jobs}
    assert names == {
        recover_inbound_events,
        recover_outbound_messages,
        expire_drafts,
        alert_unclaimed_orders,
        expire_schedule_confirmations,
        remind_still_closed,
        process_daily_reports,
        process_report_corrections,
    }


@pytest.mark.parametrize(
    ("interval", "expected"),
    [
        (30, {"second": {0, 30}}),
        (15, {"second": {0, 15, 30, 45}}),
        (1, {"second": set(range(60))}),
        (60, {"minute": set(range(60)), "second": 0}),
        (300, {"minute": {0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}, "second": 0}),
    ],
)
def test_cron_schedule(interval: int, expected: dict) -> None:
    assert cron_schedule(interval) == expected


def test_build_cron_jobs_uses_the_interval() -> None:
    jobs = build_cron_jobs(30)
    assert all(job.second == {0, 30} and job.unique for job in jobs)


@pytest.mark.parametrize(
    ("attempts", "seconds"), [(1, 2), (2, 4), (3, 8), (4, 16), (8, 256), (9, 300), (20, 300)]
)
def test_backoff_delay(attempts: int, seconds: int) -> None:
    assert backoff_delay(attempts) == timedelta(seconds=seconds)


def test_rate_limit_window_is_per_sender_and_minute() -> None:
    a = "549@s.whatsapp.net"
    assert window_key(a, now=120.0) == window_key(a, now=179.9)
    assert window_key(a, now=179.9) != window_key(a, now=180.0)
    assert window_key(a, now=120.0) != window_key("550@s.whatsapp.net", now=120.0)


def test_catalog_admin_outbound_failed_matches_doc_07() -> None:
    text = catalog.render(
        catalog.ADMIN_OUTBOUND_FAILED,
        template_key="group_order_assigned",
        recipient_name="el grupo de moteros",
        attempts_text=catalog.attempts_text(5),
    )
    assert text == (
        "❗ No se pudo enviar el mensaje `group_order_assigned` a el grupo de moteros "
        "tras 5 intentos."
    )


@pytest.mark.parametrize(
    ("attempts", "expected"),
    [(1, "1 intento"), (2, "2 intentos"), (5, "5 intentos"), (11, "11 intentos")],
)
def test_attempts_text_singular_and_plural(attempts: int, expected: str) -> None:
    assert catalog.attempts_text(attempts) == expected


def test_admin_outbound_failed_singular_for_one_attempt() -> None:
    text = catalog.render(
        catalog.ADMIN_OUTBOUND_FAILED,
        template_key="store_order_published",
        recipient_name="Pizzería Test",
        attempts_text=catalog.attempts_text(1),
    )
    assert text.endswith("a Pizzería Test tras 1 intento.")
    assert "intentos" not in text
    assert "intento(s)" not in text


def test_catalog_rejects_missing_and_unknown_variables() -> None:
    with pytest.raises(KeyError):
        catalog.render(catalog.ADMIN_RATE_LIMITED_SENDER)
    with pytest.raises(ValueError):
        catalog.render(catalog.ADMIN_RATE_LIMITED_SENDER, sender_name="X", extra=1)
